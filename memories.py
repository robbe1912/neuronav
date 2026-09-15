"""memories.py — Serena-style project memories (issue #67).

Plain markdown files under ``<state_dir>/memories/`` — one per named
memory. The deterministic index answers "what IS"; memories answer
"what we LEARNED" ("the flaky test is X", "deploys go through Y") —
the one store that makes an agent smarter across sessions.

Laws (rival census top-5 #2):
- plain files only: no index/store coupling, rescan never touches this
  dir, and it rides nav's state_dir resolution for free — a routed
  ``dir`` call serves that project's memories because STATE_DIR is
  rebound for the call's duration (issue #131);
- UTF-8 no BOM, LF verbatim: writes are bytes through a same-dir temp
  + os.replace (atomic, no newline translation), reads tolerate a BOM
  but never emit one (portability law);
- deterministic: names sort plain-codepoint, no timestamps, no unordered
  iteration — same data -> same bytes;
- loud failures: an undecodable or convention-breaking file errors
  naming the file (never a silent skip in list/get), and delete refuses
  a missing name instead of reporting success.

File convention (hand-edits are first-class, discovered by list): line 1
is ``# <name>``; optionally a one-line ``<!-- summary -->`` comment
right after (what list shows); the rest is the body, verbatim.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import nav

MEMORIES_DIR = "memories"
README_NAME = "README.md"
SUMMARY_CHARS = 120  # one-line summary cap in list output

# safe filename charset: must start alphanumeric (blocks "", ".", "..",
# ".hidden" and any absolute shape), then letters/digits/._- — this
# refuses path separators, wildcards, quotes, spaces and control chars
# outright, so a validated name can only ever name one file in the
# memories dir (never a traversal: "../x" contains "/")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Windows reserved device names: "con.md" is unopenable there, and a
# trailing dot is stripped by Win32 ("name." == "name")
_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

_COMMENT_RE = re.compile(r"^<!--\s*(.*?)\s*-->$")

README_TEXT = """\
# neuronav memories

One markdown file per memory (issue #67, the Serena convention). The
`memory` MCP tool is the interface:

- `memory(verb="list")` — names + one-line summaries
- `memory(verb="get", name="...")` — full body
- `memory(verb="set", name="...", body="...")` — create/overwrite
- `memory(verb="delete", name="...")` — remove

File convention (hand-edits welcome — `list` discovers them): line 1 is
`# <name>`, optionally followed by a one-line `<!-- summary -->`
comment that `list` shows; the rest is the body, stored verbatim by
`set`. UTF-8, LF endings, no BOM. This README is scaffolding, not a
memory — `list` skips it, and the name `README` is reserved.
"""


def dir_path() -> Path:
    """The active config's memories dir — read at call time so a routed
    call (nav.config_scope) serves that project's memories."""
    return nav.STATE_DIR / MEMORIES_DIR


def scaffold(state_dir: Path) -> Path:
    """Create ``<state_dir>/memories`` + the convention README. Shared
    by onboard.scaffold and init: one literal, byte-identical either
    way. Existence-guarded like .neuroignore — a user-customized README
    is never rewritten."""
    d = state_dir / MEMORIES_DIR
    d.mkdir(parents=True, exist_ok=True)
    rd = d / README_NAME
    if not rd.is_file():
        rd.write_bytes(README_TEXT.encode())
    return rd


def _validate_name(name: str) -> None:
    """Refuse anything that is not one safe filename component."""
    if (
        not NAME_RE.match(name)
        or name.endswith(".")
        or name.upper() in _RESERVED
        or name.upper() == "README"
    ):
        raise ValueError(
            f"memory name {name!r} is not a safe filename — use 1-128 chars of "
            "letters/digits/._- starting alphanumeric, not ending in '.', and "
            "not a reserved name (README, CON, NUL, ...)"
        )


def _path(name: str) -> Path:
    # only reached after _validate_name: the join cannot escape the dir
    return dir_path() / f"{name}.md"


def _parse(path: Path) -> str:
    """Decode + H1-split one memory file; returns the body after the
    ``# <name>`` line, verbatim (so set->get round-trips byte-identical).
    Loud on corruption: undecodable bytes or a missing H1 name the
    file — never a silent skip."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(
            f"memory file {path.as_posix()} is not valid UTF-8 — fix or delete it ({e})"
        ) from e
    text = text.removeprefix("\ufeff")  # BOM-tolerant read; never written
    head, _, rest = text.partition("\n")
    if not head.startswith("# "):
        raise ValueError(
            f"memory file {path.as_posix()} breaks the convention — first line "
            f"must be '# <name>' (got {head[:40]!r}); fix or delete it"
        )
    return rest


def _cap(line: str) -> str:
    return line if len(line) <= SUMMARY_CHARS else line[:SUMMARY_CHARS - 3] + "..."


def _summary(body: str) -> str:
    """First non-empty line after the H1: an HTML comment unwraps to its
    text (the summary convention), anything else is shown as-is."""
    for ln in body.splitlines():
        s = ln.strip()
        if not s:
            continue
        m = _COMMENT_RE.match(s)
        return _cap(m.group(1) if m else s)
    return "-"


def _atomic_write(path: Path, data: bytes) -> None:
    """Same-dir temp + os.replace (the onboard.py law): a crash never
    leaves a truncated memory over a good one. Binary mode — LF stays
    LF, no BOM, no translation."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".mem-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def list_memories() -> str:
    d = dir_path()
    if not d.is_dir():
        return "no memories yet — create one with memory(verb=\"set\", name=..., body=...)"
    names = sorted(
        p.name for p in d.iterdir() if p.is_file() and p.suffix == ".md"
    )
    rows = []
    for fn in names:
        if fn == README_NAME:  # scaffolding, not a memory
            continue
        rows.append(f"{Path(fn).stem}: {_summary(_parse(d / fn))}")
    if not rows:
        return "no memories yet — create one with memory(verb=\"set\", name=..., body=...)"
    n = len(rows)
    return (
        f"{n} memor{'y' if n == 1 else 'ies'} in {d.as_posix()}\n" + "\n".join(rows)
    )


def get_memory(name: str) -> str:
    _validate_name(name)
    path = _path(name)
    if not path.is_file():
        raise ValueError(
            f"no memory named '{name}' in {dir_path().as_posix()} — list shows what exists"
        )
    return _parse(path)


def set_memory(name: str, body: str) -> str:
    _validate_name(name)
    if not body:
        raise ValueError("set refuses an empty body — delete the memory instead")
    path = _path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = f"# {name}\n".encode() + body.encode()
    _atomic_write(path, data)
    return f"saved '{name}' ({len(data)} bytes) — {path.as_posix()}"


def delete_memory(name: str) -> str:
    _validate_name(name)
    path = _path(name)
    if not path.is_file():
        raise ValueError(
            f"no memory named '{name}' in {dir_path().as_posix()} — refusing to "
            "delete what does not exist; list shows what exists"
        )
    path.unlink()
    return f"deleted '{name}' ({path.as_posix()})"


VERBS = ("list", "get", "set", "delete")


def run(verb: str, name: str, body: str) -> str:
    """The memory tool body (server.py wraps this in _route)."""
    if verb == "list":
        return list_memories()
    if verb == "get":
        return get_memory(name)
    if verb == "set":
        return set_memory(name, body)
    if verb == "delete":
        return delete_memory(name)
    raise ValueError(
        f"unknown memory verb {verb!r} — use one of: {', '.join(VERBS)}"
    )
