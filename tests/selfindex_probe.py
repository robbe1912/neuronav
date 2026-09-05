# self-index sanity: semantic recall + cluster cross-talk on gdnav's own code
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nav

nav._apply_config(nav.TOOL_DIR / "config" / "gdnav.json")

print("== search: force-directed node layout ==")
for h in nav.search("force-directed node layout spring physics", n=4):
    print(f"  {h.score:.3f} {h.path}")

print("== search: language extractor contract ==")
for h in nav.search("language extractor plugin contract FileSym", n=4):
    print(f"  {h.score:.3f} {h.path}")

print("== clusters (cross-talk check) ==")
for c in nav.clusters():
    dirs = sorted({p.split("/")[0] if "/" in p else "(root)" for p, _ in c["paths"]})
    print(f"  [{c['label']}] n={c['size']} dirs={dirs}")
