# tests/test_langsep — language-separation law for shared modules.
# Run in its own process:
#   .venv/Scripts/python.exe -X utf8 tests/test_langsep.py
#
# The law (owner mandate): shared, language-agnostic modules contain ZERO
# language-conditioned behavior. Every language-specific fact — file
# suffixes, name patterns, entry rules, parse quirks, res:// handling —
# lives in extractors/ behind the package registry (extractors/__init__.py)
# and is consumed blind via uniform hooks. A new language plugs in with a
# new extractor module + registry entry and NO diff in shared files.
#
# Scan set: graph.py nav.py server.py viz.py layout.py clusters.py
# explore.py recall.py onboard.py bake/*.py. tools/ joins when ServeGuard's
# #120 lands (it holds 4 .tscn sites — LJ-4; growing the set is a one-line
# change and must not be forgotten).
#
# Temporary, line-anchored allowlist: clusters.py is PR 2 of this wave
# (owner ruling) and viz.py:2838 is queued behind HarnessPro's #123/#89.
# Every allowlisted line is individually anchored and REQUIRES a live
# detector hit — a stale entry fails this suite, so PR 2 cannot merge
# without deleting its block. No blanket file carve-outs exist.
#
# Hermetic: text pins read source only; the tier pin builds a graph over
# tests/fixtures/langsep with a generated temp config + temp state dir and
# NEURONAV_EMBED_FAKE=1 — no live store, config-agnostic.
import json
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)



SHARED = sorted(
    [HERE / n for n in ("graph.py", "nav.py", "server.py", "viz.py", "layout.py",
                        "clusters.py", "explore.py", "recall.py", "onboard.py")]
    + list((HERE / "bake").glob("*.py"))
)

# quoted language-suffix literals (code form; prose comments never quote them)
SUFFIX_LIT = re.compile(r"""["']\.(?:gd|tscn|tres|res|py|h|hpp|cc|cxx)["']""")
# ext/suffix comparison idioms — the per-language branch shapes. NOTE: no \b
# after the symbol operators (both neighbors are non-word); \b guards only
# the word-ending `in` forms so `include`-style words never match.
GUARD_IDIOMS = re.compile(
    r"""(?:\.ext\s*(?:==|!=|not\s+in\b|in\b)|suffix\s*(?:==|!=|not\s+in\b|in\b)|in\s+CPP_EXTS\b)"""
)
# glob literals handed to rglob/glob/iter_root_files — `*.tscn` is a
# suffix fact in disguise and escapes the quoted-suffix detector
GLOB_LIT = re.compile(r"""["']\*\.(?:gd|tscn|tres|res|py|h|hpp|cc|cxx)["']""")
PSEUDO_LIT = re.compile(r"""["']::(?:tscn|SIGNAL:|VAR:)["']""")
# the one legal home for the pseudo spellings: their frozen constant defs
GRAMMAR_DEF = re.compile(r"^(?:FN_KEY_SEP|TSCN_SUFFIX|SIGNAL_PREFIX|VAR_PREFIX)\s*=")
# res:// handled as LOGIC (strip / prefix-test / regex) rather than display
RES_LIT = re.compile(r"""["']res://""")
RES_VERB = re.compile(
    r"""(?:startswith|endswith|removeprefix|removesuffix|\[len\(|\.replace\(|\.split\(|\.search\(|\.match\(|re\.compile)"""
)
# JS-side suffix regexes in the embedded viz template (viz.py:2838 class)
JS_SUFFIX = re.compile(r"""/\\.(?:tscn|gd|tres|res|py|cpp|h|hpp)\b""")

# ---- temporary, line-anchored allowlist (see header) ---------------------------
ALLOWED = {}
for _ln in (173, 181, 186, 194, 211, 219, 280, 469, 484, 641, 743, 903, 966,
            992, 1080, 1095, 1132, 1175, 1230, 1323, 1337, 1341):
    ALLOWED[("clusters.py", _ln)] = "PR2: clusters refactor queued this wave"
ALLOWED[("viz.py", 2838)] = "V-1: queued behind #123/#89 (data-flag contract)"
# config/parametric walk filters — EXTS is the user's config include-set
# and `suffixes` arrives as a caller argument (registry datum at the call
# site); neither is a language truth hard-coded in nav
ALLOWED[("nav.py", 467)] = "config walk filter (EXTS = user config)"
ALLOWED[("nav.py", 497)] = "parametric walk filter (caller-supplied suffixes)"
ALLOWED[("nav.py", 527)] = "config walk filter (EXTS = user config)"


def detectors(line: str) -> list[str]:
    why = []
    if SUFFIX_LIT.search(line):
        why.append("suffix")
    if GUARD_IDIOMS.search(line):
        why.append("guard")
    if PSEUDO_LIT.search(line):
        why.append("pseudo")
    if RES_LIT.search(line) and RES_VERB.search(line):
        why.append("reslogic")
    if JS_SUFFIX.search(line):
        why.append("jssuffix")
    if GLOB_LIT.search(line):
        why.append("glob")
    return why


hits: dict[str, list[str]] = {}
used_allowlist: set = set()
for f in SHARED:
    rel = f.relative_to(HERE).as_posix()
    for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
        why = detectors(line)
        if "pseudo" in why and rel == "graph.py" and GRAMMAR_DEF.match(line):
            why.remove("pseudo")
        if why and (rel, i) in ALLOWED:
            used_allowlist.add((rel, i))
            continue
        for kind in why:
            hits.setdefault(kind, []).append(f"{rel}:{i}: {line.strip()[:90]}")

for kind in ("suffix", "guard", "pseudo", "reslogic", "jssuffix", "glob"):
    label = {
        "suffix": "no bare suffix literals in shared modules",
        "guard": "no ext/suffix guard idioms in shared modules",
        "pseudo": "fn-key pseudo spellings only at their graph.py owner",
        "reslogic": "no res:// logic-strip idioms in shared modules",
        "jssuffix": "no JS suffix-regex leaks in the viz template",
        "glob": "no glob literals over language suffixes in shared modules",
    }[kind]
    found = hits.get(kind, [])
    check(label, not found, f"{len(found)} site(s)" + ("; first: " + found[0] if found else ""))

stale = sorted(k for k in ALLOWED if k not in used_allowlist)
check("allowlist fully live (no stale entries)", not stale,
      f"stale: {stale}" if stale else f"{len(ALLOWED)} anchored deferrals")
check("clusters.py deferral is anchored per-line, not blanket",
      all(isinstance(k, tuple) and k[0] == "clusters.py" for k in ALLOWED if k[0] == "clusters.py"))

# ---- pin 2: package-only import surface ----------------------------------------
DEEP_IMPORT = re.compile(r"""(?:from\s+extractors\.[\w.]+\s+import|^import\s+extractors\.)""")
hits_import: list[str] = []
for f in SHARED:
    rel = f.relative_to(HERE).as_posix()
    for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
        if DEEP_IMPORT.search(line):
            hits_import.append(f"{rel}:{i}: {line.strip()[:90]}")
check("shared modules import extractors via the package only", not hits_import,
      f"{len(hits_import)} site(s)" + ("; first: " + hits_import[0] if hits_import else ""))

# ---- pin 3: registry completeness (uniform hook contract) ----------------------
sys.path.insert(0, str(HERE))
from extractors import EXTENSIONS, registry_for  # noqa: E402

REQUIRED = ("parse", "ENTRY_RULES", "scan_file", "DYNAMIC_HINT",
            "is_entry_exempt", "unresolved_base_review", "stand_in_review",
            "mention_review")
bad_contract = [
    f".{sfx}" for sfx, mod in sorted(EXTENSIONS.items())
    if not all(hasattr(mod, attr) for attr in REQUIRED)
]
check("every EXTENSIONS entry exposes the uniform hook contract",
      not bad_contract, ", ".join(bad_contract) or f"{len(EXTENSIONS)} suffixes OK")

check("registry_for round-trips every registered suffix",
      all(registry_for(s) is EXTENSIONS[s] for s in EXTENSIONS))

try:
    from extractors import WIRING_ONLY_SUFFIXES  # noqa: E402
except ImportError:
    WIRING_ONLY_SUFFIXES = None
check("registry exports WIRING_ONLY_SUFFIXES", WIRING_ONLY_SUFFIXES is not None,
      "extractors/__init__.py must export it (wiring-only suffixes like .tscn)")
if WIRING_ONLY_SUFFIXES is not None:
    check("WIRING_ONLY_SUFFIXES are registered suffixes",
          WIRING_ONLY_SUFFIXES <= set(EXTENSIONS),
          f"stray: {sorted(WIRING_ONLY_SUFFIXES - set(EXTENSIONS))}" or "OK")

# ---- pin 4 (LJ-3): cross-language VIRTUALS shield survives the hook split ------
# A .py fn named like a Godot virtual on an unresolved base stays "likely";
# an un-shielded underscore fn and a py do_* stand-in land "review".
FIX = HERE / "tests" / "fixtures" / "langsep"
cfg = Path(tempfile.gettempdir()) / "neuronav_langsep_tier_config.json"
state = Path(tempfile.mkdtemp(prefix="neuronav_langsep_tier_"))
cfg.write_text(
    json.dumps({
        "root": str(FIX), "collection": "langsep_tier", "state_dir": str(state),
        "include_dirs": ["."], "extensions": [".py"], "exclude_dirs": [],
    }),
    encoding="utf-8",
)
os.environ["NEURONAV_CONFIG"] = str(cfg)
os.environ.setdefault("NEURONAV_EMBED_FAKE", "1")

import graph  # noqa: E402  (binds the fixture config via NEURONAV_CONFIG)

g = graph.get_graph(rebuild=True)
rows = {r["func"]: r["tier"] for r in g.dead_code()["candidates"]}
check("LJ-3: py _process on unresolved base stays 'likely' (VIRTUALS shield)",
      rows.get("_process") == "likely", f"got {rows.get('_process')!r}")
check("LJ-3: py _mystery_thing on unresolved base lands 'review'",
      rows.get("_mystery_thing") == "review", f"got {rows.get('_mystery_thing')!r}")
check("LJ-3: py do_get stand-in lands 'review'",
      rows.get("do_get") == "review", f"got {rows.get('do_get')!r}")

# ---- summary -------------------------------------------------------------------
print()
if FAILS:
    print(f"FAILED {len(FAILS)} check(s): " + ", ".join(FAILS))
    sys.exit(1)
print("langsep: all checks passed")
