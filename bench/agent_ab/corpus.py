"""Deterministic checkout walk shared by the grep arm and task derivation.

Models what a plain shell agent sees: `grep -rn --include='*.py' .` from the
repo root. Prunes only VCS/tool caches — unlike the nav config profile
(which additionally excludes bench/ and other dirs from the INDEX), a shell
has no ignore profile: tests/ and bench/ are right there in the checkout
and every grep pays for every line of them. That asymmetry is part of the
measurement, not a bug in it (README, "fairness").
"""
from __future__ import annotations

import re
from pathlib import Path

# nobody greps into VCS/venv/tool caches
PRUNE = {".git", ".venv", ".chroma", ".tmp", "__pycache__",
         "node_modules", ".neuronav"}
PY_SUFFIX = ".py"

DEF_RE = re.compile(r"^[ \t]*(?:async )?def ([A-Za-z_]\w*)\(")


def walk_py(root: Path) -> list[str]:
    """Repo-relative .py paths over the whole checkout, sorted (deterministic
    walk order — the harness never iterates an unsorted tree)."""
    out: list[str] = []
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for entry in entries:
            if entry.name in PRUNE:
                continue
            if entry.is_dir():
                stack.append(entry)
            elif entry.suffix == PY_SUFFIX:
                out.append(entry.relative_to(root).as_posix())
    out.sort()
    return out


def read_lines(root: Path, rel: str) -> list[str]:
    """`cat` a tracked .py file; lenient decode like grep over mixed content."""
    return (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()


def def_index(root: Path) -> dict[str, set[str]]:
    """name -> defining files over the whole checkout. Used ONLY by task
    derivation (an unambiguity gate — a question with two right answers
    measures nothing); the grep arm pays for its own scans at run time."""
    idx: dict[str, set[str]] = {}
    for rel in walk_py(root):
        for line in read_lines(root, rel):
            m = DEF_RE.match(line)
            if m:
                idx.setdefault(m.group(1), set()).add(rel)
    return idx
