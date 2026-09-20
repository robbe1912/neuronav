# Span-aware owner_at adoption tooth (issue #377) — run in its own process:
#   NEURONAV_EMBED_FAKE=1 .venv/Scripts/python.exe -X utf8 tests/test_owner_span.py
#
# c.py:438 fixed the container() closure's line attribution (sites
# attribute only within [def line, body end]; TU-scope sites after the
# last fn must attribute to nothing) but the fix never propagated to
# the other spellings. #377 hoists common.owner_at with the FIXED
# semantics and adopts it everywhere. The fixture families pin
# byte-identity, so this suite is the dedicated tooth for the one
# intentional behavior delta: a trailing module/TU-scope site after the
# last fn's body —
#   tsx: module-scope JSX render site -> component root (not a call
#        edge from the preceding fn)
#   jsx: same, js dialect
#   cpp: TU-scope initializer call -> no edge from the preceding fn
# Under the OLD last-def-line-<= closure every check below fails (the
# trailing site minted an edge from the last fn); under common.owner_at
# they pass. Determinism leg: double build -> identical digest.
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

SRC = {
    "app.tsx": (
        "// local component, NOT exported: entry hints must not mask the\n"
        "// span-aware attribution delta (exported PascalCase gets its own\n"
        "// entry-hint root regardless of container semantics)\n"
        "function App(): void {\n"
        "  return;\n"
        "}\n"
        "\n"
        "function last(): number {\n"
        "  return 1;\n"
        "}\n"
        "\n"
        "// module-scope render site AFTER last()'s body — must NOT\n"
        "// attribute to last()\n"
        "createRoot(document.getElementById(\"root\")!).render(<App />);\n"
    ),
    "panel.jsx": (
        "function Panel() {\n"
        "  return null;\n"
        "}\n"
        "\n"
        "function last() {\n"
        "  return 1;\n"
        "}\n"
        "\n"
        "// module-scope render site AFTER last()'s body\n"
        "createRoot(document.getElementById(\"root\")).render(<Panel />);\n"
    ),
    "app.ts": (
        "export function App(): void {\n"
        "  return;\n"
        "}\n"
    ),
    "main.tsx": (
        "import { App } from \"./app\";\n"
        "\n"
        "function last(): number {\n"
        "  return 1;\n"
        "}\n"
        "\n"
        "// module-scope render of an IMPORTED component AFTER last()'s\n"
        "// body — under the old closure this minted a bogus call edge\n"
        "// from last() to the component's file\n"
        "createRoot(document.getElementById(\"root\")!).render(<App />);\n"
    ),
    "comp.js": (
        "export function Panel() {\n"
        "  return null;\n"
        "}\n"
    ),
    "main.jsx": (
        "import { Panel } from \"./comp\";\n"
        "\n"
        "function last() {\n"
        "  return 1;\n"
        "}\n"
        "\n"
        "createRoot(document.getElementById(\"root\")).render(<Panel />);\n"
    ),
    "trailing.cpp": (
        "static int helper(void) {\n"
        "  return 1;\n"
        "}\n"
        "\n"
        "static int other(void) {\n"
        "  return 2;\n"
        "}\n"
        "\n"
        "static int last(void) {\n"
        "  return other();\n"
        "}\n"
        "\n"
        "// TU-scope initializer AFTER last()'s body — must NOT attribute\n"
        "// to last()\n"
        "static int READY = helper();\n"
    ),
}

TMP = Path(tempfile.mkdtemp(prefix="neuronav_ownerspan_"))
CORPUS = TMP / "corpus"
CORPUS.mkdir(parents=True)
for name, text in SRC.items():
    (CORPUS / name).write_text(text, encoding="utf-8")
(TMP / "config.json").write_text(json.dumps({
    "root": str(CORPUS),
    "collection": "ownerspan",
    "include_dirs": ["."],
    "extensions": [".ts", ".tsx", ".js", ".jsx", ".cpp"],
    "exclude_dirs": [],
    "state_dir": str(TMP / "state"),
}), encoding="utf-8")
os.environ["NEURONAV_CONFIG"] = str(TMP / "config.json")
os.environ.setdefault("NEURONAV_EMBED_FAKE", "1")

import graph  # noqa: E402  (binds the corpus config via NEURONAV_CONFIG)

from harness import check, finish  # noqa: E402


def digest(gr):
    return (
        sorted((k, sorted(v)) for k, v in gr.edges.items()),
        sorted((c["path"], c["func"], c["tier"])
               for c in gr.dead_code(100)["candidates"]),
        sorted(gr.roots),
    )


g = graph.Graph().build()
edges = {f"{s} -> {t}" for s, tgts in g.edges.items() for t in tgts}

# ---- tsx: trailing JSX render site lands at module scope ------------------------
check("tsx: module-scope <App/> render roots the local component",
      "app.tsx::App" in g.roots, str(sorted(g.roots)))
check("tsx: no bogus call edge from last() to the module-scope render",
      "app.tsx::last -> app.tsx::App" not in edges, str(sorted(edges)))

# ---- jsx: same shape, js dialect -----------------------------------------------
check("jsx: module-scope <Panel/> render roots the local component",
      "panel.jsx::Panel" in g.roots, str(sorted(g.roots)))
check("jsx: no bogus call edge from last() to the module-scope render",
      "panel.jsx::last -> panel.jsx::Panel" not in edges, str(sorted(edges)))

# ---- imported render target: the bogus-edge shape the old closure minted --------
check("tsx: no bogus edge from last() to the imported render target",
      "main.tsx::last -> app.ts::App" not in edges, str(sorted(edges)))
check("jsx: no bogus edge from last() to the imported render target",
      "main.jsx::last -> comp.js::Panel" not in edges, str(sorted(edges)))

# ---- cpp: TU-scope initializer call attributes to nothing ----------------------
check("cpp: no bogus edge from last() to the TU-scope initializer call",
      "trailing.cpp::last -> trailing.cpp::helper" not in edges,
      str(sorted(edges)))
check("cpp: the genuine in-body call edge survives",
      "trailing.cpp::last -> trailing.cpp::other" in edges, str(sorted(edges)))

# ---- determinism ---------------------------------------------------------------
g2 = graph.Graph().build()
check("double build byte-identical (edges + dead + roots)",
      digest(g) == digest(g2))

import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
finish()
