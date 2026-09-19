# bake/gitinfo — pure per-job transforms for viz._build_data (issue #86
# phase-2 V8). Moved verbatim from viz.py; every nav/graph/chroma edge
# stays in the viz.py orchestrator — data arrives as arguments.

import subprocess
from collections import defaultdict
from pathlib import Path

def head(repo_dir) -> str:
    """Short HEAD hash of the neuronav repo for the freshness stamp."""
    try:
        got = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",  # issue #118: git speaks UTF-8 — never the locale
        )
        return got.stdout.strip()
    except Exception:
        return ""


def churn(paths: list[str], root) -> list[float] | None:
    """Per-file git churn of the TARGET project, normalized to 0..1.

    Counts how often each indexed file appears in the last 90 days of
    commits (`git log --name-only --since=90.days`) at navconfig.ROOT — the
    scanned game repo, not the neuronav tooling repo. Git prints paths
    relative to the repo top level, which may sit above ROOT, so those
    are rebased onto ROOT before matching node paths. Returns None
    (channel disabled — no visual change) when git or history is
    unavailable.
    """
    try:
        root = str(root)
        # git emits UTF-8 paths; without an explicit encoding the locale
        # codec (cp1252 on Windows) decodes them to mojibake keys that
        # never match node paths — or raises on undefined bytes, silently
        # disabling the whole churn channel. errors="replace" keeps one
        # weird path from killing the channel (issue #119).
        got = subprocess.run(
            ["git", "log", "--name-only", "--since=90.days", "--pretty=format:"],
            cwd=root,
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",  # issue #118
        )
        if got.returncode != 0:
            return None
        pre = ""
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=root,
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",  # issue #118
        ).stdout.strip().replace("\\", "/")
        if top:
            try:
                pre = Path(root).resolve().relative_to(Path(top).resolve()).as_posix()
                if pre in (".", ""):
                    pre = ""
                else:
                    pre += "/"
            except ValueError:
                pre = ""
        touches: dict[str, int] = defaultdict(int)
        for line in got.stdout.splitlines():
            line = line.strip().replace("\\", "/")
            if line and line.startswith(pre):
                touches[line[len(pre):]] += 1
        if not touches:
            return None
        mx = max(touches.values())
        return [round(touches.get(p, 0) / mx, 3) for p in paths]
    except Exception:
        return None
