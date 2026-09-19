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
# explore.py recall.py onboard.py bake/*.py tools/*.py. tools/ joined with
# ServeGuard's #120 landing (LJ-4, repaid via #199: the one live site —
# qa_readability's affordance subject filter — routes through the registry
# predicate is_scene_path; tools/ carries no allowlist entries).
#
# Temporary, content-anchored allowlist (#301 B: line numbers pin
# nothing anymore — a routine upstream insert no longer needs allowlist
# surgery; the anchor is a unique signature substring instead): the viz
# template's .tscn regex is queued behind HarnessPro's #123/#89 and
# nav's walk filters are config truth (not language truth). clusters.py
# carried PR-2 deferrals until its cutover landed — it must stay clean
# now, so its block is GONE and this asserts it. Every allowlisted
# entry covers a line that still trips a detector — a signature whose
# line stops tripping (or vanishes entirely) goes stale and fails this
# suite.
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



from harness import FAILURES as FAILS, check

SHARED = sorted(
    [HERE / n for n in ("graph.py", "nav.py", "navconfig.py", "navstore.py", "navindex.py", "server.py", "viz.py", "layout.py",
                        "clusters.py", "explore.py", "recall.py", "onboard.py")]
    + list((HERE / "bake").glob("*.py"))
    + list((HERE / "vizjs").glob("*.py"))   # #299 A: the template lives here now
    + list((HERE / "tools").glob("*.py"))
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
# godot project-file section literals — repo-config facts that are still
# language-family truths (project.godot walk, [autoload] section header)
GODOT_LIT = re.compile(r"""["'](?:project\.godot|\[autoload\])["']""")
PSEUDO_LIT = re.compile(r"""["']::(?:tscn|SIGNAL:|VAR:)["']""")
# the one legal home for the pseudo spellings: their frozen constant defs
GRAMMAR_DEF = re.compile(r"^(?:FN_KEY_SEP|TSCN_SUFFIX|SIGNAL_PREFIX|VAR_PREFIX)\s*=")
# res:// handled as LOGIC (strip / prefix-test / regex) rather than display
RES_LIT = re.compile(r"""["']res://""")
RES_VERB = re.compile(
    r"""(?:startswith|endswith|removeprefix|removesuffix|\[len\(|\.replace\(|\.split\(|\.search\(|\.match\(|re\.compile)"""
)
# JS-side suffix regexes in the embedded viz template (viz.py:2783 class)
JS_SUFFIX = re.compile(r"""/\\.(?:tscn|gd|tres|res|py|cpp|h|hpp)\b""")

# ---- temporary, content-anchored allowlist (see header) ------------------------
# keys are (file, signature substring); each signature must stay unique
# in its file — a second occurrence trips the sweep below, a missing one
# goes stale. The signature pins the idiom, never its line number.
ALLOWED = {
    ("vizjs/focus_vis.py", "const tscn = fi >= 0 && /\\.tscn$/i.test"):
        "V-1: queued behind #123/#89 (data-flag contract; moved verbatim"
        " from viz.py:3086 by #299 A)",
    # config/parametric walk filters — EXTS is the user's config include-set
    # and `suffixes` arrives as a caller argument (registry datum at the
    # call site); neither is a language truth hard-coded in nav
    ("navindex.py", "if all_suffixes or Path(name).suffix in navconfig.EXTS:"):
        "config walk filter (EXTS = user config; all_suffixes=#240 census)",
    ("navindex.py", "if Path(name).suffix in suffixes:"):
        "parametric walk filter (caller-supplied suffixes)",
    ("navindex.py", "if Path(e.name).suffix not in navconfig.EXTS:"):
        "config walk filter (EXTS = user config)",
}


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
    if GODOT_LIT.search(line):
        why.append("godot")
    return why


hits: dict[str, list[str]] = {}
used_allowlist: set = set()
for f in SHARED:
    rel = f.relative_to(HERE).as_posix()
    for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
        why = detectors(line)
        if "pseudo" in why and rel == "graph.py" and GRAMMAR_DEF.match(line):
            why.remove("pseudo")
        if why:
            # #301 B: content anchor — the entry covers this hit only when
            # the line carries its signature, wherever that line lives now
            covered = next((k for k in ALLOWED if k[0] == rel and k[1] in line), None)
            if covered is not None:
                used_allowlist.add(covered)
                continue
        for kind in why:
            hits.setdefault(kind, []).append(f"{rel}:{i}: {line.strip()[:90]}")

for kind in ("suffix", "guard", "pseudo", "reslogic", "jssuffix", "glob", "godot"):
    label = {
        "suffix": "no bare suffix literals in shared modules",
        "guard": "no ext/suffix guard idioms in shared modules",
        "pseudo": "fn-key pseudo spellings only at their graph.py owner",
        "reslogic": "no res:// logic-strip idioms in shared modules",
        "jssuffix": "no JS suffix-regex leaks in the viz template",
        "glob": "no glob literals over language suffixes in shared modules",
        "godot": "no godot project/section literals in shared modules",
    }[kind]
    found = hits.get(kind, [])
    check(label, not found, f"{len(found)} site(s)" + ("; first: " + found[0] if found else ""))

stale = sorted(k for k in ALLOWED if k not in used_allowlist)
check("allowlist fully live (no stale entries)", not stale,
      f"stale: {stale}" if stale else f"{len(ALLOWED)} anchored deferrals")
check("clusters.py carries zero deferrals (PR2 cutover landed)",
      not any(k[0] == "clusters.py" for k in ALLOWED),
      f"stray: {sorted(k for k in ALLOWED if k[0] == 'clusters.py')}")

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

# derived from the consumers-of-record: graph.py (scan_file, DYNAMIC_HINT,
# is_entry_exempt, unresolved_base_review, stand_in_review, mention_review),
# nav.py (stat_tags — F3: a missing stub crashed rescan on .py corpora),
# bake/files_model.py + bake/wires.py (is_wiring_only)
REQUIRED = ("parse", "ENTRY_RULES", "scan_file", "DYNAMIC_HINT",
            "is_entry_exempt", "unresolved_base_review", "stand_in_review",
            "mention_review", "stat_tags", "is_wiring_only",
            "counts_dead_share")
bad_contract = [
    f".{sfx}" for sfx, mod in sorted(EXTENSIONS.items())
    if not all(hasattr(mod, attr) for attr in REQUIRED)
]
check("every EXTENSIONS entry exposes the uniform hook contract",
      not bad_contract, ", ".join(bad_contract) or f"{len(EXTENSIONS)} suffixes OK")

check("registry_for round-trips every registered suffix",
      all(registry_for(s) is EXTENSIONS[s] for s in EXTENSIONS))

try:
    from extractors import SCENE_FILE_SUFFIXES  # noqa: E402
except ImportError:
    SCENE_FILE_SUFFIXES = None
check("registry exports SCENE_FILE_SUFFIXES", SCENE_FILE_SUFFIXES is not None,
      "extractors/__init__.py must export it (scene-document suffixes like .tscn)")
if SCENE_FILE_SUFFIXES is not None:
    check("SCENE_FILE_SUFFIXES are registered suffixes",
          SCENE_FILE_SUFFIXES <= set(EXTENSIONS),
          f"stray: {sorted(SCENE_FILE_SUFFIXES - set(EXTENSIONS))}" or "OK")

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

# ---- pin 5 (#302): extractor front-end leaf families live in common.py --------
# The >=3-copy leaf families (node helpers, line_starts, _rel_of_target,
# the import-liveness sweep shell, the scan-body receiver prologue) are
# hoisted to extractors/common.py; ts/js/rust import them. A verbatim
# per-module spelling is drift by definition — the ts-js pair had
# already diverged before the hoist.
import extractors.common as _common  # noqa: E402
import extractors.js as _js  # noqa: E402
import extractors.rust as _rust  # noqa: E402
import extractors.ts as _ts  # noqa: E402
check("front-end leaf helpers are common.py's (identity, ts/js/rust)",
      _ts._text is getattr(_common, "node_text", None)
      and _ts._line is getattr(_common, "node_line", None)
      and _ts._rel_of_target is getattr(_common, "rel_of_target", None)
      and _js._text is getattr(_common, "node_text", None)
      and _js._line is getattr(_common, "node_line", None)
      and _js._rel_of_target is getattr(_common, "rel_of_target", None)
      and _rust._text is getattr(_common, "node_text", None)
      and _rust._line is getattr(_common, "node_line", None)
      and _rust._rel_of_target is getattr(_common, "rel_of_target", None),
      "extractors must alias common's node_text/node_line/rel_of_target")
_leaf_src = {m: (HERE / "extractors" / f"{m}.py").read_text(encoding="utf-8")
             for m in ("ts", "js", "rust")}
_verbatim = sorted(
    f"{m}.py:{pat}" for m, s in _leaf_src.items()
    for pat in ("def _text(", "def _line(", "def _ident_child(", "def _rel_of_target(")
    if pat in s
)
check("no verbatim node-helper/_rel_of_target spelling remains in ts/js/rust",
      not _verbatim, ", ".join(_verbatim) or "clean")
_hoist_src = {m: (HERE / "extractors" / f"{m}.py").read_text(encoding="utf-8")
              for m in ("ts", "js", "rust", "python")}
_unhoisted = sorted(
    m + ".py" for m, s in _hoist_src.items()
    if "make_import_liveness_sweep" not in s or "receiver_env" not in s
)
check("sweep shell + receiver prologue hoisted (ts/js/rust/python)",
      not _unhoisted, ", ".join(_unhoisted) or "all four import the shells")

# ---- summary -------------------------------------------------------------------
# summary tail is a pre-#301 byte pin (names failures)
print()
if FAILS:
    print(f"FAILED {len(FAILS)} check(s): " + ", ".join(FAILS))
    sys.exit(1)
print("langsep: all checks passed")
