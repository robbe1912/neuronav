# arch-rule engine over crosstalk QA (issue #70) — run in its own process:
#   .venv/Scripts/python.exe -X utf8 tests/test_archrules.py
#
# Hermetic: synthetic partitions + stub graphs (no index; chroma is only
# touched by the nav import) and the rules file lives in a temp state
# dir (explicit `state_dir` config field), so the suite never touches
# the checkout's .neuronav. Pins:
#   - teeth: a planted violation PER RULE KIND is caught with the exact
#     wire count + offending file pairs — sabotage the engine (make a
#     kind's evaluation go missing or vacuous) and its leg fails here
#   - typo guard: unknown kind/cluster/type/key, malformed file,
#     duplicate id -> named config errors, never silently skipped (the
#     good rules in the same file still evaluate)
#   - #114 parity: rules read clusters.cross_tallies, so they see
#     exactly what crosstalk() counts — tests/ and unclustered wiring
#     feeds no rule number
#   - determinism: identical runs render byte-identical output
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parents[1]

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


TMP = Path(tempfile.mkdtemp(prefix="neuronav_archrules_"))
cfg = TMP / "neuronav_archrules_config.json"
cfg.write_text(
    json.dumps(
        {
            "root": str(HERE),
            "collection": "archrules_fix",
            "state_dir": str(TMP),
            "include_dirs": ["tests"],
            "extensions": [".gd"],
            "exclude_dirs": [],
        }
    ),
    encoding="utf-8",
)
os.environ["NEURONAV_CONFIG"] = str(cfg)
os.environ.setdefault("NEURONAV_EMBED_FAKE", "1")
sys.path.insert(0, str(HERE))

import clusters as C  # noqa: E402  (binds the temp config via NEURONAV_CONFIG)
import nav  # noqa: E402
import archrules as A  # noqa: E402

RULES = nav.STATE_DIR / "arch-rules.json"


def write_rules(rules, extra=None):
    doc = {"rules": rules} if extra is None else {"rules": rules, **extra}
    RULES.write_text(json.dumps(doc, indent=1), encoding="utf-8")


def cluster(cid, label, paths):
    return {"id": cid, "label": label, "size": len(paths), "paths": [(p, "") for p in paths]}


CS = [
    cluster(0, "UI", ["ui/panel.gd", "ui/hud.gd"]),
    cluster(1, "Net", ["net/client.gd", "net/server.gd"]),
    cluster(2, "IO", ["io/save.gd"]),
]
# ordered cross-cluster wires: UI->Net 3, Net->UI 1, UI->IO 2 (one var,
# one call); internal UI 1; tests-endpoint 2 (touching UI and Net — the
# parity trap); unclustered-endpoint 1 (touching Net)
G = SimpleNamespace(
    edges={
        "ui/panel.gd::a": {"net/client.gd::b": 1},
        "ui/panel.gd::c": {"net/client.gd::b": 1},
        "ui/hud.gd::d": {"net/client.gd::b": 1},
        "net/server.gd::e": {"ui/panel.gd::a": 1},
        "ui/panel.gd::f": {"io/save.gd::g": 1},
        "ui/hud.gd::h": {"io/save.gd::g": 1},
        "ui/panel.gd::z": {"ui/hud.gd::d": 1},
        "ui/panel.gd::t": {"tests/t.gd::y": 1},
        "net/client.gd::t2": {"tests/t2.gd::y": 1},
        "loose/l.gd::q": {"net/client.gd::b": 1},
    },
    edge_types={
        ("ui/panel.gd::f", "io/save.gd::g"): {"var"},
        ("ui/hud.gd::h", "io/save.gd::g"): {"call"},
    },
)

# ------------------------------------------- 1. planted violations (teeth)
# one leg per rule kind: if the engine ever skips a kind (or makes its
# evaluation vacuous) the planted violation disappears and this fails
write_rules([{"id": "ui-no-net", "kind": "forbid", "from": "UI", "to": "Net"}])
rep = A.check(CS, G)
check("forbid: planted violation caught", len(rep["violations"]) == 1, str(rep["violations"]))
v = rep["violations"][0] if rep["violations"] else {}
check("forbid: exact wire count", v.get("wires") == 3, str(v.get("wires")))
check("forbid: names rule + clusters",
      v.get("rule") == "ui-no-net" and v.get("from", {}).get("label") == "UI"
      and v.get("to", {}).get("label") == "Net", str(v)[:160])
check("forbid: offending file pairs ranked",
      v.get("files") == [
          {"pair": "ui/panel.gd -> net/client.gd", "w": 2},
          {"pair": "ui/hud.gd -> net/client.gd", "w": 1},
      ], str(v.get("files")))
write_rules([{"id": "net-no-ui", "kind": "forbid", "from": "Net", "to": "UI"}])
check("forbid: direction respected (Net->UI is 1 wire, not 3)",
      A.check(CS, G)["violations"][0]["wires"] == 1)

write_rules([{"id": "ui-net-budget", "kind": "budget", "from": "UI", "to": "Net", "max": 3}])
check("budget: at-max holds (no violation)", not A.check(CS, G)["violations"])
write_rules([{"id": "ui-net-budget", "kind": "budget", "from": "UI", "to": "Net", "max": 2}])
rep = A.check(CS, G)
check("budget: over-max caught with count + limit",
      rep["violations"][0]["wires"] == 3 and rep["violations"][0]["limit"] == 2,
      str(rep["violations"])[:160])

# ------------------------------------------------- 2. typed rules
write_rules([{"id": "no-var-into-io", "kind": "forbid", "from": "UI", "to": "IO", "types": ["var"]}])
rep = A.check(CS, G)
check("types: counts only that edge kind",
      rep["violations"][0]["wires"] == 1
      and rep["violations"][0]["files"] == [{"pair": "ui/panel.gd -> io/save.gd", "w": 1}],
      str(rep["violations"])[:160])
write_rules([{"id": "no-calls-into-io", "kind": "forbid", "from": "UI", "to": "IO", "types": ["call"]}])
check("types: sibling kind is independent",
      A.check(CS, G)["violations"][0]["files"] == [{"pair": "ui/hud.gd -> io/save.gd", "w": 1}])
write_rules([{"id": "all-io", "kind": "forbid", "from": "UI", "to": "IO"}])
check("untyped rule counts both typed wires", A.check(CS, G)["violations"][0]["wires"] == 2)
untyped_stub = SimpleNamespace(edges={"ui/hud.gd::h": {"io/save.gd::g": 1}})
write_rules([{"id": "no-var-into-io", "kind": "forbid", "from": "UI", "to": "IO", "types": ["var"]}])
check("types: graph without edge info feeds no typed count",
      not A.check(CS, untyped_stub)["violations"])

# ------------------------------------- 3. severity + synthesized id
write_rules([{"id": "soft", "kind": "forbid", "from": "UI", "to": "Net", "severity": "warn"}])
v = A.check(CS, G)["violations"][0]
check("severity rides through", v["severity"] == "warn", str(v.get("severity")))
check("fmt marks severity", "[warn] soft" in A.fmt(A.check(CS, G)))
write_rules([{"kind": "forbid", "from": "UI", "to": "Net"}])
check("missing id is synthesized deterministically",
      A.check(CS, G)["violations"][0]["rule"] == "forbid UI->Net")

# ------------------------------------------------- 4. clean summary
write_rules([
    {"id": "a", "kind": "budget", "from": "UI", "to": "Net", "max": 3},
    {"id": "b", "kind": "budget", "from": "Net", "to": "UI", "max": 1},
    {"id": "c", "kind": "forbid", "from": "IO", "to": "Net"},
])
rep = A.check(CS, G)
check("clean partition: zero violations", not rep["violations"] and not rep["config_errors"], str(rep))
out = A.fmt(rep)
check("clean summary line", "arch check: 3 rule(s), clean" in out, out.splitlines()[0])
write_rules([{"id": "ui-no-net", "kind": "forbid", "from": "UI", "to": "Net"}])
out = A.fmt(A.check(CS, G))
check("violation summary line + file pairs",
      "arch check: 1 rule(s), 1 violation(s)" in out
      and "ui/panel.gd -> net/client.gd x2" in out, out.splitlines()[0])

# ------------------------------------------- 5. typo guard (loud, never skipped)
write_rules([{"id": "k", "kind": "forbidd", "from": "UI", "to": "Net"}])
rep = A.check(CS, G)
check("unknown kind: named error",
      len(rep["config_errors"]) == 1 and "'k'" in rep["config_errors"][0]
      and "forbidd" in rep["config_errors"][0] and "forbid, budget" in rep["config_errors"][0],
      str(rep["config_errors"]))
check("unknown kind: rule NOT evaluated", rep["rules"] == 0 and not rep["violations"], str(rep))
write_rules([
    {"id": "k", "kind": "forbidd", "from": "UI", "to": "Net"},
    {"id": "ui-no-net", "kind": "forbid", "from": "UI", "to": "Net"},
])
rep = A.check(CS, G)
check("mixed file: error AND violation both reported (never silently skipped)",
      len(rep["config_errors"]) == 1 and len(rep["violations"]) == 1,
      f"errs={rep['config_errors']} vs={str(rep['violations'])[:120]}")
write_rules([{"id": "u", "kind": "forbid", "from": "U I", "to": "Net"}])
err = A.check(CS, G)["config_errors"][0]
check("unknown cluster label: named + known clusters listed",
      "'u'" in err and "no cluster labelled 'U I'" in err and "c0 UI" in err and "c1 Net" in err, err)
write_rules([{"id": "u", "kind": "forbid", "from": "c9", "to": "Net"}])
check("unknown cN id: named",
      "no cluster with id c9" in A.check(CS, G)["config_errors"][0],
      str(A.check(CS, G)["config_errors"]))
amb = CS + [cluster(3, "ui", ["ui/menu.gd"])]
write_rules([{"id": "u", "kind": "forbid", "from": "Ui", "to": "Net"}])
check("ambiguous case-insensitive match is an error",
      "ambiguous" in A.check(amb, G)["config_errors"][0], str(A.check(amb, G)["config_errors"]))
write_rules([{"id": "t", "kind": "forbid", "from": "UI", "to": "Net", "types": ["varr"]}])
err = A.check(CS, G)["config_errors"][0]
check("unknown edge type: named + vocabulary listed",
      "'t'" in err and "varr" in err and "call" in err, err)
write_rules([{"id": "x", "kind": "forbid", "from": "UI", "to": "Net", "severityy": "warn"}])
check("unknown rule key: named",
      "unknown key 'severityy'" in A.check(CS, G)["config_errors"][0])
write_rules([{"id": "m", "kind": "budget", "from": "UI", "to": "Net"}])
check("budget without max: named", "requires 'max'" in A.check(CS, G)["config_errors"][0])
write_rules([{"id": "m", "kind": "budget", "from": "UI", "to": "Net", "max": -1}])
check("negative max: named", "integer >= 0" in A.check(CS, G)["config_errors"][0])
write_rules([{"id": "m", "kind": "budget", "from": "UI", "to": "Net", "max": "3"}])
check("non-int max: named", "integer >= 0" in A.check(CS, G)["config_errors"][0])
write_rules([{"id": "f", "kind": "forbid", "from": "UI", "to": "Net", "max": 0}])
check("forbid with max: told to use budget", "takes no 'max'" in A.check(CS, G)["config_errors"][0])
write_rules([{"id": "s", "kind": "forbid", "from": "UI", "to": "Net", "severity": "fatal"}])
check("unknown severity: named", "unknown severity" in A.check(CS, G)["config_errors"][0])
write_rules([
    {"id": "dup", "kind": "forbid", "from": "UI", "to": "Net"},
    {"id": "dup", "kind": "budget", "from": "Net", "to": "UI", "max": 9},
])
check("duplicate ids: named", "duplicate rule id 'dup' x2" in A.check(CS, G)["config_errors"][0])
write_rules(["not an object"])
check("non-object rule entry: named", "rule #1" in A.check(CS, G)["config_errors"][0])
write_rules([{"id": "ok", "kind": "forbid", "from": "UI", "to": "Net"}], extra={"note": "hi"})
check("unknown top-level key: named", "unknown key 'note'" in A.check(CS, G)["config_errors"][0])
RULES.write_text('["not", "an", "object"]', encoding="utf-8")
check("non-object root: named", "must be an object" in A.check(CS, G)["config_errors"][0])
RULES.write_text('{"rules": "nope"}', encoding="utf-8")
check("non-list rules: named", "'rules' must be a list" in A.check(CS, G)["config_errors"][0])
RULES.write_text('{"rules": [', encoding="utf-8")
err = A.check(CS, G)["config_errors"][0]
check("malformed JSON: named", "unreadable" in err, err)

# --------------------------------------------- 6. absent file is not an error
RULES.unlink()
rep = A.check(CS, G)
check("absent rules file: configured False, no errors",
      rep["configured"] is False and not rep["config_errors"], str(rep))
out = A.run(CS, G)
check("absent rules file: actionable answer with the write path",
      "no arch rules configured" in out and str(RULES) in out and '"forbid"' in out, out[:120])
check("empty index: same answer shape as other tools",
      A.run([], G) == "index empty — call rescan first")

# --------------------------------------- 7. parity with crosstalk (#114/#70)
rep_ct = C.crosstalk(CS, G)
check("crosstalk: external 6 on fixture", rep_ct["external_edges"] == 6, str(rep_ct["external_edges"]))
check("crosstalk: tests endpoints tallied separately", rep_ct["tests_endpoint_edges"] == 2)
check("crosstalk: unclustered endpoints tallied separately", rep_ct["unclustered_endpoint_edges"] == 1)
check("crosstalk: internal 1", rep_ct["internal_edges"] == 1)
t = C.cross_tallies(CS, G)
check("tallies: ordered pair wires",
      len(t["pair_wires"][(0, 1)]) == 3 and len(t["pair_wires"][(1, 0)]) == 1
      and len(t["pair_wires"][(0, 2)]) == 2, str({k: len(v) for k, v in t["pair_wires"].items()}))
write_rules([
    {"id": "p1", "kind": "forbid", "from": "UI", "to": "Net"},
    {"id": "p2", "kind": "forbid", "from": "Net", "to": "UI"},
    {"id": "p3", "kind": "forbid", "from": "UI", "to": "IO"},
])
rep = A.check(CS, G)
check("parity: engine wire sum == crosstalk external_edges (tests/ + unclustered never inflate)",
      sum(x["wires"] for x in rep["violations"]) == rep_ct["external_edges"] == 6,
      str([x["wires"] for x in rep["violations"]]))

# ----------------------------------------------- 8. cluster reference forms
for ref in ("Net", "net", "c1"):
    write_rules([{"id": "r", "kind": "forbid", "from": ref, "to": "UI"}])
    rep = A.check(CS, G)
    check(f"cluster ref {ref!r} resolves to the same cluster",
          rep["violations"][0]["wires"] == 1 and rep["violations"][0]["from"]["id"] == 1,
          str(rep["violations"])[:120])

# ------------------------------------------------------ 9. determinism
write_rules([
    {"id": "b-budget", "kind": "budget", "from": "Net", "to": "UI", "max": 0},
    {"id": "a-forbid", "kind": "forbid", "from": "UI", "to": "Net"},
    {"id": "c-var", "kind": "forbid", "from": "UI", "to": "IO", "types": ["var"]},
])
r1, r2 = A.check(CS, G), A.check(CS, G)
check("determinism: identical reports",
      json.dumps(r1, sort_keys=True, default=str) == json.dumps(r2, sort_keys=True, default=str))
check("determinism: byte-identical render", A.fmt(r1) == A.fmt(r2))
check("violations sorted severity-first then rule id",
      [x["rule"] for x in r1["violations"]] == ["a-forbid", "b-budget", "c-var"],
      str([x["rule"] for x in r1["violations"]]))

# ------------------------------------- 10. rules follow the routed state dir
saved = nav.STATE_DIR
try:
    other = TMP / "other_state"
    other.mkdir()
    nav.STATE_DIR = other
    check("rules_path reads nav.STATE_DIR at call time (routing law)",
          A.rules_path() == other / "arch-rules.json", str(A.rules_path()))
    check("routed project without rules: not configured",
          A.check(CS, G)["configured"] is False)
    (other / "arch-rules.json").write_text(
        json.dumps({"rules": [{"id": "r", "kind": "budget", "from": "UI", "to": "Net", "max": 99}]}),
        encoding="utf-8",
    )
    rep = A.check(CS, G)
    check("routed rules file is the one evaluated",
          rep["configured"] and rep["rules"] == 1 and not rep["violations"], str(rep))
finally:
    nav.STATE_DIR = saved

# --------------------- 11. scene->scene wiring feeds no rule number (#267)
# cross_tallies carves scene->scene resource references (pack composition)
# into their own counter; rules read the same tallies, so a rule sees no
# composition wiring either — while scene<->script wiring (attach) stays
# rule-visible, mirroring the crosstalk report exactly (#114).
CS_SC = [
    cluster(5, "Hub", ["main.tscn", "hub/ctl.gd"]),
    cluster(6, "Pack", ["pack/a.tscn", "pack/s.gd"]),
]
G_SC = SimpleNamespace(
    edges={
        "main.tscn::tscn": {
            "pack/a.tscn::tscn": 1,  # scene -> scene inst: composition
            "pack/s.gd::ready": 1,  # scene -> script attach: coupling
        },
    },
    edge_types={
        ("main.tscn::tscn", "pack/a.tscn::tscn"): {"inst"},
        ("main.tscn::tscn", "pack/s.gd::ready"): {"attach"},
    },
)
t_sc = C.cross_tallies(CS_SC, G_SC)
check("tallies: scene->scene carved into own counter (#267)",
      t_sc.get("scene_scene") == 1 and len(t_sc["pair_wires"].get((5, 6), [])) == 1,
      str(t_sc.get("scene_scene")))
rep_sc = C.crosstalk(CS_SC, G_SC)
check("crosstalk: scene composition feeds no cluster number",
      rep_sc["external_edges"] == 1 and rep_sc.get("scene_scene_edges") == 1,
      f"ext {rep_sc['external_edges']}")
write_rules([
    {"id": "sc-inst", "kind": "forbid", "from": "Hub", "to": "Pack", "types": ["inst"]},
    {"id": "sc-any", "kind": "forbid", "from": "Hub", "to": "Pack"},
])
rep = A.check(CS_SC, G_SC)
by_rule = {x["rule"]: x["wires"] for x in rep["violations"]}
check("rules see no scene->scene wires but keep scene->script (#267/#114)",
      by_rule.get("sc-any") == 1 and "sc-inst" not in by_rule,
      str(rep["violations"])[:120])

print()
if FAILS:
    print(f"{len(FAILS)} FAIL: {FAILS}")
    sys.exit(1)
print("archrules: all checks passed")
