"""Task corpus for the agent-level A/B (issue #72).

Each task = a natural prompt + a verifiable expected answer derived from the
LIVE index (g.files / g.edges / g.reverse / g.dead_code) — never hardcoded,
so the corpus tracks the repo it measures. Selection is deterministic
(sorted candidates, first K) and every class gates on index shape, skipping
loudly when this repo cannot exercise it (the test_viz issue #97 pattern:
an interaction the corpus cannot produce is reported, not dropped).

Unambiguity gate: every targeted symbol must have exactly one `def` across
the whole CHECKOUT (not just the index) — a question with two defensible
answers measures nothing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import corpus

CLASSES = ("find-symbol", "trace-call-path", "locate-refactor-site",
           "dead-code-check")

# symbol_graph's callers/callees rows cap (graph._fmt_node slices [:8]);
# answers that would overflow the tool output are not eligible tasks.
GRAPH_ROW_CAP = 8

_NESTED_DEF_RE = re.compile(r"^[ \t]*(?:async )?def ", re.MULTILINE)


@dataclass
class Task:
    cls: str
    tid: str
    prompt: str
    expected: object
    facts: dict = field(default_factory=dict)  # target symbols for playbooks

    def check(self, answer: object) -> bool:
        """Ground-truth verdict — the harness's teeth. Exact on every
        dimension: wrong file, a partial or padded callee set, or the wrong
        dead tier all fail; None (no answer) fails. No partial credit."""
        return _norm(self.expected) == _norm(answer)


def _norm(v: object) -> object:
    if isinstance(v, (list, tuple, set, frozenset)):
        return tuple(sorted(set(v)))
    return v


def _file(key: str) -> str:
    return key.partition("::")[0]


def _name(key: str) -> str:
    return key.partition("::")[2]


def fn_keys(g) -> list[str]:
    """All fn keys ("path::name"), sorted."""
    out = []
    for rel in sorted(g.files):
        for name in sorted(g.files[rel].funcs):
            out.append(f"{rel}::{name}")
    return sorted(out)


def _is_fn(g, key: str) -> bool:
    f, n = _file(key), _name(key)
    return f in g.files and n in g.files[f].funcs


def derive_tasks(g, root: Path, per_class: int = 3,
                 dead_each: int = 2) -> tuple[list[Task], list[str]]:
    """(tasks, notes). notes carry every loud SKIP/NOTE; a class yields zero
    tasks only when the index genuinely cannot exercise it."""
    notes: list[str] = []
    tasks: list[Task] = []
    defs = corpus.def_index(root)

    def unique(name: str) -> bool:
        return len(defs.get(name, ())) == 1

    # -- find-symbol: "which file defines fn X?" -----------------------------
    got: list[Task] = []
    for key in fn_keys(g):
        rel, name = _file(key), _name(key)
        if name == "main" or len(name) < 4 or not unique(name):
            continue
        if not g.reverse.get(key):
            continue  # want a used symbol, not any def
        got.append(Task(
            "find-symbol", f"find-symbol:{key}",
            f"Which file in this repo defines the function `{name}`? "
            "Reply with the repo-relative path only.",
            rel, {"name": name, "file": rel}))
        if len(got) >= per_class:
            break
    _collect(tasks, notes, got, "find-symbol", per_class)

    # -- trace-call-path: "what does fn X call in other files?" --------------
    got = []
    for key in fn_keys(g):
        if key not in g.edges:
            continue
        rel, name = _file(key), _name(key)
        callees = g.edges[key]
        cross = sorted(c for c in callees
                       if _file(c) != rel and _is_fn(g, c))
        if not cross or len(cross) > 6 or len(callees) > GRAPH_ROW_CAP:
            continue
        if not unique(name) or not all(unique(_name(c)) for c in cross):
            continue
        body = g.files[rel].funcs[name].body
        if len(_NESTED_DEF_RE.findall(body)) > 1:
            continue  # nested defs make textual callee attribution ambiguous
        got.append(Task(
            "trace-call-path", f"trace-call-path:{key}",
            f"The function `{name}` in `{rel}` calls functions defined in "
            "other files. Which ones? List each as `path::func`.",
            cross, {"name": name, "file": rel}))
        if len(got) >= per_class:
            break
    _collect(tasks, notes, got, "trace-call-path", per_class)

    # -- locate-refactor-site: "a signature change — which files break?" -----
    got = []
    for key in fn_keys(g):
        callers = g.reverse.get(key)
        if not callers or len(callers) > GRAPH_ROW_CAP:
            continue
        rel, name = _file(key), _name(key)
        if not unique(name):
            continue
        others = sorted({_file(c) for c in callers} - {rel})
        if not 1 <= len(others) <= 5:
            continue
        got.append(Task(
            "locate-refactor-site", f"locate-refactor-site:{key}",
            f"We are changing the signature of `{name}` (defined in "
            f"`{rel}`). Which OTHER files in this repo contain call sites "
            "that must be updated? List the repo-relative paths.",
            others, {"name": name, "file": rel}))
        if len(got) >= per_class:
            break
    _collect(tasks, notes, got, "locate-refactor-site", per_class)

    # -- dead-code-check: "alive or dead?" (both sides of the answer) --------
    got = []
    cands = g.dead_code(limit=1_000_000)["candidates"]
    for cand in cands[:dead_each]:
        path, func = cand["path"], cand["func"]
        if not unique(func):
            continue
        got.append(Task(
            "dead-code-check", f"dead-code-check:{path}::{func}",
            f"Is the function `{func}` defined in `{path}` reachable from "
            "any entry point of this repo (test entry points count)? Answer "
            "exactly `alive` or `dead:<tier>` (tier: likely|review).",
            f"dead:{cand['tier']}", {"name": func, "file": path}))
    if not cands:
        notes.append("SKIP dead-code-check: index has no dead-code candidates")
    elif not got:
        notes.append(
            f"SKIP dead-code-check: none of the first {dead_each} dead "
            "candidates has a checkout-unique name")
    elif len(got) < dead_each:
        notes.append(
            f"NOTE dead-code-check: dead side thinned {len(got)}/{dead_each} "
            "(non-unique candidate names skipped)")
    alive = 0
    for key in fn_keys(g):
        if alive >= dead_each:
            break
        if key not in g.reachable or g.reverse.get(key):
            continue  # alive side wants reachability WITHOUT static callers
        name = _name(key)
        if not unique(name):
            continue
        got.append(Task(
            "dead-code-check", f"dead-code-check:{key}",
            f"Is the function `{name}` defined in `{_file(key)}` reachable "
            "from any entry point of this repo (test entry points count)? "
            "Answer exactly `alive` or `dead:<tier>` (tier: likely|review).",
            "alive", {"name": name, "file": _file(key)}))
        alive += 1
    if alive < dead_each:
        notes.append(
            f"NOTE dead-code-check: alive-no-caller side thinned {alive}/"
            f"{dead_each} (few root fns with checkout-unique names)")
    if got:
        tasks += got
    elif cands:
        notes.append("SKIP dead-code-check: neither side exercisable")

    return tasks, notes


def _collect(tasks: list[Task], notes: list[str], got: list[Task],
             cls: str, want: int) -> None:
    if not got:
        notes.append(f"SKIP {cls}: no eligible fn in this index shape")
    else:
        tasks += got
        if len(got) < want:
            notes.append(f"NOTE {cls}: thinned {len(got)}/{want}")
