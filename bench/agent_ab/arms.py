"""The two arms of the agent-level A/B (issue #72).

Each arm is a SCRIPTED POLICY: a fixed tool-call playbook per task class,
parameterized only by the task's target symbols — never by its expected
answer. No LLM is in the loop, so cost-to-answer measures the TOOLS, not
model cleverness, and every run is reproducible (README, "model-free").

Arm A ("grep"): shell text tools only — grep / cat / ls over the checkout,
no index, no semantic anything. Arm B ("neuronav"): direct in-process calls
mirroring the MCP read tools (repo_map / find_functions / symbol_graph /
dead_code); the MCP layer reshapes some of that output (caps, headers,
views), so the byte counts here are direct-API bytes, not stdio wire
bytes (README, "direct vs stdio").
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import corpus
from .tasks import Task


@dataclass
class Cost:
    calls: int = 0        # tool invocations
    files: int = 0        # files read/scanned by the arm's tools
    read_bytes: int = 0   # bytes the tools pulled off disk
    ret_bytes: int = 0    # bytes the tools returned to the "agent" —
                          # the context-cost proxy (model-free: no tokens)


# ---- arm A: shell text tools -----------------------------------------------

# The grep playbook's fixed prior knowledge: Python keywords, builtins, and
# common stdlib method names it must not chase as callee definitions. This
# is knowledge a competent shell agent walks in with — pinned here so runs
# are reproducible.
PY_NOCHASE = frozenset({
    # keywords
    "if", "elif", "else", "for", "while", "return", "def", "class", "lambda",
    "not", "in", "is", "and", "or", "try", "except", "finally", "with", "as",
    "assert", "raise", "yield", "await", "async", "import", "from", "global",
    "nonlocal", "pass", "break", "continue", "del", "match", "case",
    # builtins
    "print", "len", "range", "str", "int", "float", "bool", "set", "dict",
    "list", "tuple", "sorted", "reversed", "min", "max", "sum", "any", "all",
    "enumerate", "zip", "open", "isinstance", "issubclass", "super", "type",
    "abs", "repr", "format", "getattr", "setattr", "hasattr", "callable",
    "vars", "dir", "id", "hash", "iter", "next", "map", "filter", "bytes",
    "bytearray", "frozenset", "complex", "divmod", "pow", "round", "hex",
    "oct", "bin", "chr", "ord", "compile", "eval", "exec", "input", "exit",
    "quit", "help", "staticmethod", "classmethod", "property",
    # stdlib / builtin method names — chasing these is noise, not signal
    "get", "items", "keys", "values", "append", "extend", "insert", "pop",
    "update", "add", "discard", "join", "split", "splitlines", "strip",
    "lstrip", "rstrip", "search", "sub", "findall", "group", "groups",
    "encode", "decode", "read", "readlines", "write", "writelines", "close",
    "count", "index", "find", "lower", "upper", "title", "capitalize",
    "startswith", "endswith", "replace", "copy", "clear", "remove", "sort",
    "exists", "isdir", "isfile", "mkdir", "makedirs", "unlink", "rename",
    "glob", "rglob", "iterdir", "resolve", "relative_to", "as_posix",
    "read_text", "write_text", "with_suffix", "is_relative_to", "fromkeys",
    "setdefault", "difference", "intersection", "union",
    "symmetric_difference", "seek", "tell", "flush",
})

CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")


class ShellTools:
    """grep / cat / ls with honest cost accounting. One grep invocation
    walks (and pays for) the whole checkout — exactly like `grep -rn`."""

    def __init__(self, root: Path):
        self.root = root
        self.cost = Cost()

    def grep(self, pattern: str) -> list[tuple[str, int, str]]:
        """`grep -rn PATTERN --include='*.py' .` → (path, 1-based line,
        line text) in walk order."""
        rx = re.compile(pattern)
        rels = corpus.walk_py(self.root)
        self.cost.calls += 1
        self.cost.files += len(rels)
        out: list[tuple[str, int, str]] = []
        for rel in rels:
            lines = corpus.read_lines(self.root, rel)
            self.cost.read_bytes += sum(len(ln) + 1 for ln in lines)
            for i, ln in enumerate(lines, 1):
                if rx.search(ln):
                    out.append((rel, i, ln))
                    self.cost.ret_bytes += len(ln) + 1
        return out

    def read(self, rel: str) -> list[str] | None:
        """`cat` a file (the read a grep agent does once it has a hit)."""
        try:
            lines = corpus.read_lines(self.root, rel)
        except OSError:
            return None
        self.cost.calls += 1
        self.cost.files += 1
        size = sum(len(ln) + 1 for ln in lines)
        self.cost.read_bytes += size
        self.cost.ret_bytes += size
        return lines

    def ls(self, rel: str = ".") -> list[str]:
        """`ls` a directory."""
        self.cost.calls += 1
        names = sorted(p.name for p in (self.root / rel).iterdir())
        self.cost.ret_bytes += sum(len(n) + 1 for n in names)
        return names


def _def_line_re(name: str) -> re.Pattern:
    return re.compile(rf"^[ \t]*(?:async )?def {re.escape(name)}\(")


def _fn_body(sh: ShellTools, rel: str, name: str) -> list[str] | None:
    """The def line plus its indented block, by indent level (how a shell
    agent slices a function out of a cat dump)."""
    lines = sh.read(rel)
    if lines is None:
        return None
    start = next((i for i, ln in enumerate(lines)
                  if _def_line_re(name).match(ln)), None)
    if start is None:
        return None
    indent = len(lines[start]) - len(lines[start].lstrip())
    body = [lines[start]]
    for ln in lines[start + 1:]:
        if ln.strip():
            li = len(ln) - len(ln.lstrip())
            if li <= indent:
                break
        body.append(ln)
    return body


def grep_playbook(task: Task, sh: ShellTools) -> object:
    """Fixed regex playbook per class — the deterministic grep-agent."""
    cls, name, file = task.cls, task.facts["name"], task.facts["file"]
    if cls == "find-symbol":
        ms = sh.grep(_def_line_re(name).pattern)
        return ms[0][0] if ms else None
    if cls == "trace-call-path":
        body = _fn_body(sh, file, name)
        if body is None:
            return None
        cands: list[str] = []
        seen: set[str] = set()
        for ln in body:
            for m in CALL_RE.finditer(ln):
                n = m.group(1)
                if n not in seen and n not in PY_NOCHASE:
                    seen.add(n)
                    cands.append(n)
        out: list[str] = []
        for cand in cands:
            hits = sorted({m[0] for m in sh.grep(_def_line_re(cand).pattern)}
                          - {file})
            out += [f"{f}::{cand}" for f in hits]
        return out
    if cls == "locate-refactor-site":
        # call-site mentions: any `name(` that is not the def itself
        ms = sh.grep(rf"(?<!def )\b{re.escape(name)}\s*\(")
        return sorted({m[0] for m in ms} - {file})
    if cls == "dead-code-check":
        # the honest textual heuristic: a call-site mention anywhere keeps
        # it alive; only an unmentioned def reads as dead (tier unknowable
        # without reachability analysis — the arm always answers `likely`)
        ms = sh.grep(rf"(?<!def )\b{re.escape(name)}\s*\(")
        return "alive" if ms else "dead:likely"
    raise ValueError(f"unknown task class {cls}")


# ---- arm B: neuronav read tools (direct API, MCP-equivalent) ----------------

NODE_RE = re.compile(r"^(\S+)#(\S+)$")
ROW_RE = re.compile(r"^[ \t]+(callers|callees): (.*)$")


def parse_symbol_graph(text: str) -> dict[str, dict[str, list[str]]]:
    """path#name -> {"callers": [...], "callees": [...]} from symbol_graph's
    strict line grammar (graph._fmt_node: `path#name` header, 4-space rows,
    ', '-joined `path#name` shorts, '-' when empty)."""
    out: dict[str, dict[str, list[str]]] = {}
    cur = None
    for line in text.splitlines():
        m = NODE_RE.match(line)
        if m:
            cur = m.group(0)
            out[cur] = {"callers": [], "callees": []}
            continue
        m = ROW_RE.match(line)
        if m and cur is not None:
            val = [] if m.group(2) == "-" else \
                [s.strip() for s in m.group(2).split(",")]
            out[cur][m.group(1)] = val
    return out


class NavTools:
    """The neuronav-wired arm. Direct in-process calls, not a stdio server
    spawn: the harness needs byte-exact double-run determinism and honest
    per-call byte accounting, and a subprocess adds boot/pipe timing noise
    (see README, "direct vs stdio"). Counts are direct-API bytes — the
    server layer caps and reshapes output for several tools, so stdio
    wire bytes would differ modestly; call semantics mirror the served
    surface."""

    def __init__(self):
        import graph  # binds the harness's booted config (nav already bound)
        self._graph = graph
        self.cost = Cost()

    def repo_map(self, budget_tokens: int = 1024) -> str:
        self.cost.calls += 1
        text = self._graph.repo_map(budget_tokens=budget_tokens)
        self.cost.ret_bytes += len(text)
        return text

    def find_functions(self, query: str, n: int = 10) -> list[dict]:
        self.cost.calls += 1
        rows = self._graph.find_functions(query, n=n)
        # counted bytes exclude the live-embed "score" float — queries
        # re-embed per call, so fp jitter (5126 vs 5121 observed
        # cross-run) would leak into the determinism contract — and pin
        # row order by key so near-tie rank flips don't move the count.
        stable = sorted(
            ({k: v for k, v in r.items() if k != "score"} for r in rows),
            key=lambda r: r.get("key", ""))
        self.cost.ret_bytes += len(json.dumps(stable, sort_keys=True))
        return rows

    def symbol_graph(self, symbol: str, depth: int = 1) -> str:
        self.cost.calls += 1
        text = self._graph.get_graph().symbol_graph(symbol, depth=depth)
        self.cost.ret_bytes += len(text)
        return text

    def dead_code(self, limit: int = 1_000_000) -> dict:
        self.cost.calls += 1
        rows = self._graph.get_graph().dead_code(limit=limit)
        self.cost.ret_bytes += len(json.dumps(rows, sort_keys=True))
        return rows


def _block_file(entry: str) -> str:
    return entry.split("#", 1)[0]


def nav_playbook(task: Task, nv: NavTools) -> object:
    """Fixed tool-call sequence per class — the deterministic wired agent.
    Every session orients with one repo_map first (what production agents
    do), then resolves. Semantic ranks are jitter-sensitive, so answers
    derive from exact-name filtering / structural resolution, never from
    raw rank order (README, "determinism")."""
    nv.repo_map(1024)
    cls, name, file = task.cls, task.facts["name"], task.facts["file"]
    if cls == "find-symbol":
        for row in nv.find_functions(name, n=10):
            if row.get("func") == name:  # exact-name hit: rank-order-proof
                return row["path"]
        blocks = parse_symbol_graph(nv.symbol_graph(name))  # escalate
        return file if f"{file}#{name}" in blocks else None
    if cls == "trace-call-path":
        blocks = parse_symbol_graph(nv.symbol_graph(name, depth=1))
        blk = blocks.get(f"{file}#{name}")
        if blk is None:
            return None
        return sorted(k.replace("#", "::") for k in blk["callees"]
                      if _block_file(k) != file)
    if cls == "locate-refactor-site":
        blocks = parse_symbol_graph(nv.symbol_graph(name, depth=1))
        blk = blocks.get(f"{file}#{name}")
        if blk is None:
            return None
        return sorted({_block_file(k) for k in blk["callers"]} - {file})
    if cls == "dead-code-check":
        blocks = parse_symbol_graph(nv.symbol_graph(name, depth=1))
        blk = blocks.get(f"{file}#{name}")
        if blk is None:
            return None
        if blk["callers"]:
            return "alive"
        for cand in nv.dead_code()["candidates"]:
            if cand["path"] == file and cand["func"] == name:
                return f"dead:{cand['tier']}"
        return "alive"  # reachable without static callers (root/referenced)
    raise ValueError(f"unknown task class {cls}")


# ---- battery ----------------------------------------------------------------

def make_grep_arm(root: Path) -> Callable[[Task], tuple[object, Cost]]:
    def run(task: Task) -> tuple[object, Cost]:
        sh = ShellTools(root)
        return grep_playbook(task, sh), sh.cost
    return run


def make_nav_arm() -> Callable[[Task], tuple[object, Cost]]:
    def run(task: Task) -> tuple[object, Cost]:
        nv = NavTools()
        return nav_playbook(task, nv), nv.cost
    return run


def _jsonable(v: object) -> object:
    if isinstance(v, (set, frozenset, tuple)):
        return sorted(v)
    return v


def run_battery(tasks: list[Task],
                arms: dict[str, Callable[[Task], tuple[object, Cost]]]
                ) -> list[dict]:
    """Every task × every arm (arm order = dict insertion: grep, neuronav).
    Success is the task's ground-truth check — never the arm's say-so."""
    rows: list[dict] = []
    for t in tasks:
        for arm, run in arms.items():
            t0 = time.perf_counter()
            answer, cost = run(t)
            rows.append({
                "task": t.tid,
                "cls": t.cls,
                "arm": arm,
                "success": bool(t.check(answer)),
                "answer": _jsonable(answer),
                "calls": cost.calls,
                "files": cost.files,
                "read_bytes": cost.read_bytes,
                "ret_bytes": cost.ret_bytes,
                "wall_ms": round((time.perf_counter() - t0) * 1000.0, 3),
            })
    return rows


STABLE_FIELDS = ("task", "cls", "arm", "success", "answer",
                 "calls", "files", "read_bytes", "ret_bytes")


def compare_rows(rows_a: list[dict], rows_b: list[dict]) -> list[str]:
    """Field-level diffs over the deterministic projection; wall_ms is
    advisory and excluded. Empty list = double-run determinism holds."""
    out: list[str] = []
    if len(rows_a) != len(rows_b):
        return [f"row count {len(rows_a)} vs {len(rows_b)}"]
    for a, b in zip(rows_a, rows_b):
        for f in STABLE_FIELDS:
            if a.get(f) != b.get(f):
                out.append(f"{a['task']} [{a['arm']}]: {f} {a.get(f)!r} "
                           f"vs {b.get(f)!r}")
    return out
