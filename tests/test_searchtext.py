# search_text tool (issue #68) — Zoekt-style capped regex text search.
# Run in its own process:
#   .venv/Scripts/python.exe -X utf8 tests/test_searchtext.py
#
# Hermetic: generated temp target tree + config (never the real index),
# NEURONAV_EMBED_FAKE=1 (deterministic hash embeddings, no Ollama).
# Pins the contract: file:line:matched-line rows, deterministic order
# (path then line), hard caps 20 files / 3 lines each with per-file and
# global truncation markers + total counts, files_only, glob filter,
# graceful invalid-regex/empty-pattern/no-match paths, readOnlyHint.
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]

TMP = Path(tempfile.mkdtemp(prefix="neuronav_searchtext_"))
(TMP / "src").mkdir(parents=True)

(TMP / "src" / "alpha.py").write_text(
    "# needlealpha header\n"
    "def alpha_one():\n"
    "    sharedneedle = 1\n"
    "    return sharedneedle\n",
    encoding="utf-8",
)
(TMP / "src" / "beta.py").write_text(
    "x = 2  # needlebeta\n"
    "def beta_one():\n"
    "    return sharedneedle\n",
    encoding="utf-8",
)
# 6 matches -> per-file cap (3 shown, "... and 3 more matches")
BIG = "".join(f"val{i} = manyneedle + {i}\n" for i in range(6))
(TMP / "src" / "big.py").write_text(BIG, encoding="utf-8")
# 300-char matched line -> row truncation at 200 chars
(TMP / "src" / "gamma.py").write_text(
    "longneedle " + "x" * 300 + "\n", encoding="utf-8"
)
# 25 files with 1 match each -> global 20-file cap fires
for i in range(25):
    (TMP / "src" / f"cap{i:02d}.py").write_text(
        f"flag{i} = capneedle\n", encoding="utf-8"
    )
N_FILES = 29  # alpha, beta, big, gamma + 25 cap files

CFG = Path(tempfile.gettempdir()) / "neuronav_searchtext_config.json"
CFG.write_text(
    json.dumps(
        {
            "root": str(TMP),
            "collection": "searchtext",
            "include_dirs": ["src"],
            "extensions": [".py"],
            "state_dir": str(TMP / "state"),
        }
    ),
    encoding="utf-8",
)
os.environ["NEURONAV_CONFIG"] = str(CFG)
os.environ.setdefault("NEURONAV_EMBED_FAKE", "1")
sys.path.insert(0, str(HERE))

import nav  # noqa: E402  (binds the temp config above)
import server  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def rows(out: str) -> list[str]:
    """file:line:text rows only (header/footer/markers stripped)."""
    import re as _re

    return [
        ln
        for ln in out.splitlines()
        if _re.match(r"^src/[^\s:]+:\d+:", ln)
    ]


def main() -> None:
    stats = nav.rescan()
    nav.stat_mark_synced()
    check(
        "bootstrap: fixture tree indexed",
        stats["added"] == N_FILES and nav.count() == N_FILES,
        str(stats),
    )

    # ---- MCP surface: advertised, read-only ------------------------------
    async def _tools():
        return await server.mcp.list_tools()

    tools = asyncio.run(_tools())
    ann = {t.name: t.annotations for t in tools}
    check("advertised on MCP surface", "search_text" in ann, str(sorted(ann)))
    check(
        "readOnlyHint set",
        ann.get("search_text") is not None
        and ann["search_text"].readOnlyHint is True,
        str(ann.get("search_text")),
    )

    # ---- basic rows + deterministic order --------------------------------
    out = server.search_text("sharedneedle")
    expect = [
        "src/alpha.py:3:    sharedneedle = 1",
        "src/alpha.py:4:    return sharedneedle",
        "src/beta.py:3:    return sharedneedle",
    ]
    check(
        "rows are file:line:text in path-then-line order",
        rows(out) == expect,
        out,
    )
    check("footer carries totals", out.splitlines()[-1] == "3 matches in 2 files", out.splitlines()[-1])
    check("header says where we are", out.splitlines()[0].startswith("you are here: "), out.splitlines()[0])
    check("byte-stable on repeat call", server.search_text("sharedneedle") == out)

    # regex character class, not plain substring
    out = server.search_text("needle[ab]")
    check(
        "regex patterns work",
        rows(out)
        == [
            "src/alpha.py:1:# needlealpha header",
            "src/beta.py:1:x = 2  # needlebeta",
        ],
        out,
    )

    # ---- per-file cap (3 lines) -------------------------------------------
    out = server.search_text("manyneedle")
    check(
        "per-file cap: 3 rows shown",
        rows(out) == [f"src/big.py:{i + 1}:val{i} = manyneedle + {i}" for i in range(3)],
        out,
    )
    check(
        "per-file truncation marker",
        "… and 3 more matches in src/big.py" in out,
        out,
    )
    check("per-file footer counts all 6", out.splitlines()[-1] == "6 matches in 1 file", out.splitlines()[-1])

    # ---- global cap (20 files) + total count ------------------------------
    out = server.search_text("capneedle")
    cap_rows = rows(out)
    check("global cap: exactly 20 files shown", len({r.split(":")[0] for r in cap_rows}) == 20, str(len(cap_rows)))
    check(
        "global cap: deterministic first/last files",
        cap_rows[0].startswith("src/cap00.py:1:")
        and cap_rows[-1].startswith("src/cap19.py:1:"),
        f"{cap_rows[0]} .. {cap_rows[-1]}",
    )
    check("global truncation marker", "… truncated at 20 files:" in out, out.splitlines()[-1])
    check(
        "truncation footer carries true totals",
        out.splitlines()[-1]
        == "… truncated at 20 files: 25 matches in 25 files — narrow the pattern or pass glob=",
        out.splitlines()[-1],
    )
    check("byte-stable when capped", server.search_text("capneedle") == out)

    # ---- files_only: paths only, still capped ------------------------------
    out = server.search_text("capneedle", files_only=True)
    body = out.splitlines()[1:]
    check(
        "files_only: bare paths, no line rows",
        body[:-1] == [f"src/cap{i:02d}.py" for i in range(20)] and all(":" not in p for p in body[:-1]),
        out,
    )
    check("files_only: truncation footer kept", body[-1].startswith("… truncated at 20 files:"), body[-1])

    # ---- glob filter --------------------------------------------------------
    out = server.search_text("needle", glob="*alpha*")
    check(
        "glob narrows to matching paths",
        {r.split(":")[0] for r in rows(out)} == {"src/alpha.py"},
        out,
    )
    out = server.search_text("needle", glob="*nomatch*")
    check(
        "glob excluding everything reports the narrowed scan",
        out.startswith("no matches for ") and out.endswith("in 0 indexed files"),
        out,
    )

    # ---- long-line truncation ----------------------------------------------
    out = server.search_text("longneedle")
    r = rows(out)
    check(
        "long lines truncated to 200 chars + ellipsis",
        len(r) == 1 and r[0].endswith("…") and len(r[0]) <= len("src/gamma.py:1:") + 201,
        r[0][:80] + f"... ({len(r[0])} chars)" if r else out,
    )

    # ---- graceful paths ------------------------------------------------------
    check(
        "no-match message names pattern + scanned count",
        server.search_text("zzz_absent_token")
        == f"no matches for 'zzz_absent_token' in {N_FILES} indexed files",
    )
    bad = server.search_text("(unclosed")
    check("invalid regex is graceful", bad.startswith("invalid regex '(unclosed'"), bad)
    empty = server.search_text("")
    check("empty pattern is graceful", empty.startswith("no pattern given"), empty)


if __name__ == "__main__":
    main()
    print(f"{len(FAILS)} failure(s)")
    sys.exit(1 if FAILS else 0)
