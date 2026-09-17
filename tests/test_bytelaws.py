# byte-level output laws (issue #124) — run in its own process:
#   .venv/Scripts/python.exe -X utf8 tests/test_bytelaws.py
#
# Two laws that had no enforcement anywhere:
#
# 1. viz._importmap is pinned against the vendored bytes. test_bakeint
#    proves the map is SPLICED (token gone, data: URIs present) but never
#    that it decodes to the right modules — a mutated rewrite rule or a
#    dropped addon would ship a graph.html that fails only at boot, in a
#    browser, far from CI. This leg is stdlib-shaped: read the five
#    vendored files, decode each data: URI, and assert byte-equality post
#    CRLF->LF normalization plus the relative-specifier rewrite contract
#    (data: modules cannot resolve '../' — the addons' relative imports
#    must come out as their importmap keys).
# 2. The UTF-8-no-BOM write law: every JSON artifact the tooling
#    generates is asserted BOM-free at the byte level — the onboard
#    config scaffold, the export_base manifest, and the baked
#    graph.html (DATA + importmap splices). Reads are BOM-TOLERANT
#    (utf-8-sig, test_portability) exactly so writes may never emit one;
#    until now nothing refused a utf-8-sig regression on the write side.
#
# Hermetic: scratch corpus + FAKE store under the suite's own mkdtemp
# (never the real index). The importmap leg reads vendored bytes only.
import base64
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]

TMP = Path(tempfile.mkdtemp(prefix="neuronav_bytelaws_"))
SRC = TMP / "src"
SRC.mkdir(parents=True)
for i in range(4):
    (SRC / f"node_{i}.gd").write_text(
        f"extends Node\n# module {i}\nfunc work_{i}() -> int:\n\treturn {i}\n",
        encoding="utf-8",
    )
(SRC / "main.tscn").write_text('[gd_scene]\n[node name="Main" type="Node2D"]\n', encoding="utf-8")

CFG = TMP / "config.json"
CFG.write_text(
    json.dumps(
        {
            "root": str(SRC),
            "collection": "bytelaws",
            "state_dir": "default",
            "include_dirs": ["."],
            "extensions": [".gd", ".tscn"],
            "exclude_dirs": [],
        }
    ),
    encoding="utf-8",
)
os.environ["NEURONAV_CONFIG"] = str(CFG)
os.environ.setdefault("NEURONAV_EMBED_FAKE", "1")
sys.path.insert(0, str(HERE))

import nav  # noqa: E402  (binds the scratch config above)
import viz  # noqa: E402



from harness import check, finish

BOM = b"\xef\xbb\xbf"


def no_bom(label: str, path: Path) -> None:
    b = path.read_bytes()
    check(f"no-BOM law: {label}", not b.startswith(BOM), f"{path.name} starts {b[:8]!r}")


# ---- 1. _importmap pinned against the vendored bytes -------------------------
# the pin names the five modules the template imports — adding or dropping
# a vendored addon must fail HERE (loudly, at the right layer), not as a
# browser boot error. Expected bytes are computed from vendor/ with the
# CRLF->LF embed law applied; the rewrite expectation is spelled out as
# literal specifier pairs, not by re-running the implementation's regex.
map_str = viz._importmap()
check("importmap: strict JSON, no BOM", not map_str.startswith("\ufeff"),
      f"first char {map_str[:1]!r}")
im = json.loads(map_str)
check("importmap: single 'imports' member", set(im) == {"imports"}, sorted(im))

VEND = HERE / "vendor" / "three-0.160.0"
PINNED = {  # importmap key -> vendored file (the template's import surface)
    "three": "three.module.js",
    "three/addons/controls/OrbitControls.js": "controls/OrbitControls.js",
    "three/addons/lines/LineSegments2.js": "lines/LineSegments2.js",
    "three/addons/lines/LineSegmentsGeometry.js": "lines/LineSegmentsGeometry.js",
    "three/addons/lines/LineMaterial.js": "lines/LineMaterial.js",
}
# LineSegments2 is the one addon with relative imports; data: modules
# cannot resolve them, so each must come out as its importmap key
REWRITE = {
    "from '../lines/LineSegmentsGeometry.js'": "from 'three/addons/lines/LineSegmentsGeometry.js'",
    "from '../lines/LineMaterial.js'": "from 'three/addons/lines/LineMaterial.js'",
}

check("importmap: exactly the five vendored modules",
      set(im.get("imports", {})) == set(PINNED),
      f"got={sorted(im.get('imports', {}))}")

decoded: dict[str, str] = {}
for key in sorted(PINNED):
    uri = im.get("imports", {}).get(key, "")
    ok_uri = uri.startswith("data:text/javascript;base64,")
    check(f"importmap[{key}]: data URI shape", ok_uri, uri[:40])
    if not ok_uri:
        continue
    try:
        decoded[key] = base64.b64decode(uri.split(",", 1)[1]).decode("utf-8")
    except Exception as e:  # noqa: BLE001  (check-style reporting)
        check(f"importmap[{key}]: base64 decodes", False, f"{type(e).__name__}: {e}")
        continue
    want = (VEND / PINNED[key]).read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
    for frm, to in REWRITE.items():
        want = want.replace(frm, to)
    check(f"importmap[{key}]: vendored bytes, CRLF->LF, rewritten",
          decoded[key] == want,
          f"decoded={len(decoded[key])}B expected={len(want)}B")

seg2 = decoded.get("three/addons/lines/LineSegments2.js", "")
check("importmap: LineSegments2 imports its siblings by importmap key",
      all(to in seg2 for to in REWRITE.values()), "")
check("importmap: no relative specifier survives in any addon",
      all("from '../" not in js for k, js in decoded.items() if k != "three"),
      str([k for k, js in decoded.items() if k != "three" and "from '../" in js]))
check("importmap: deterministic across calls", viz._importmap() == map_str, "")

# ---- 2. UTF-8-no-BOM law on generated JSON artifacts --------------------------
# (a) this suite's own config — the law applies to every JSON artifact a
#     suite writes, this one included (not vacuously excluded)
no_bom("suite-written config", CFG)

# (b) the onboard config scaffold (onboard.scaffold -> .neuronav/config.json)
import onboard  # noqa: E402

proj = TMP / "proj"
onboard_scaffold = onboard.scaffold(proj)
no_bom("onboard config scaffold", onboard_scaffold)
check("onboard scaffold wrote the project config",
      onboard_scaffold == proj / ".neuronav" / "config.json"
      and json.loads(onboard_scaffold.read_text(encoding="utf-8-sig"))["collection"] == "main",
      str(onboard_scaffold))

# (c) the export_base manifest (nav.export_base -> base/manifest.json)
nav.rescan()
check("scratch corpus indexed", nav.count() == 5, str(nav.count()))
m = nav.export_base()
check("export produced a manifest", m["count"] == 5 and m["shards"] == 1, str(m))
no_bom("export_base manifest", nav.BASE_DIR / nav.MANIFEST_NAME)

# (d) the bake: graph.html carries the DATA + importmap JSON splices.
#     json.dumps is ASCII-escaped and the vendored JS rides base64, so a
#     BOM byte sequence anywhere in the file can only be a writer bug —
#     a utf-8-sig regression in _atomic_write would put one at byte 0.
bake = viz.generate()
html = bake.read_bytes()
check("bake wrote graph.html", bake.is_file(), str(bake))
check("no-BOM law: bake graph.html (DATA + importmap splices)",
      not html.startswith(BOM) and BOM not in html,
      f"first bytes {html[:8]!r}")

finish()
