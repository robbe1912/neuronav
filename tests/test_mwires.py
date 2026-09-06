# named-wire map exports QA (map-spec-v2 §0) — run in its own process:
#   .venv/Scripts/python.exe -X utf8 tests/test_mwires.py
#
# Builds the graph over tests/fixtures/mwires ONLY (generated config, so no
# self-index pollution), then drives viz._build_data twice and asserts the
# mwires/fns/meta contract: call rows ride the exact fedges filters, member
# (var) wires land on ::VAR: pseudo-node edges, scene connections resolve
# via the scripts cascade with unresolved ones counted, the fn roster is
# complete for a sampled file, and two builds are identical modulo the
# freshness stamp. fedges/fio shape guards pin the §0 "untouched" promise.
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
FIX = HERE / "tests" / "fixtures" / "mwires"

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


cfg = Path(tempfile.gettempdir()) / "neuronav_mwires_config.json"
cfg.write_text(
    json.dumps(
        {
            "root": str(FIX),
            "collection": "mwires_fix",
            "include_dirs": ["."],
            "extensions": [".gd", ".tscn"],
            "exclude_dirs": [],
        }
    ),
    encoding="utf-8",
)
os.environ["NEURONAV_CONFIG"] = str(cfg)
os.environ.setdefault("NEURONAV_EMBED_FAKE", "1")
sys.path.insert(0, str(HERE))

import graph  # noqa: E402  (binds the fixture config via NEURONAV_CONFIG)
import viz  # noqa: E402

g = graph.get_graph(rebuild=True)
d1 = viz._build_data()
d2 = viz._build_data()

nodes = {n["path"]: n["id"] for n in d1["nodes"]}
mw = d1["mwires"]
P, C, M = nodes["player.gd"], nodes["combat.gd"], nodes["main.tscn"]
H, E = nodes["hud.gd"], nodes["extra.gd"]

# 1. call rows ride the fedges filters: exact same wire set, type-prefixed
calls = [w for w in mw if w[0] == "call"]
check(
    "call rows mirror fedges",
    {tuple(w) for w in calls}
    == {("call", *fe, None) for fe in d1["fedges"]},
    f"{len(calls)} call rows vs {len(d1['fedges'])} fedges",
)

# 2. known call + member wires present with real def lines
strike_ln = g.files["combat.gd"].funcs["strike"].line
tune_ln = g.files["combat.gd"].funcs["tune_shield"].line
check(
    "call wire combat->player",
    ["call", C, "strike", P, "take_damage", strike_ln, None] in calls,
    str(calls),
)
vars_ = [w for w in mw if w[0] == "var"]
check(
    "var wire health member",
    ["var", C, "strike", P, "health", strike_ln, None] in vars_,
    str(vars_),
)
check(
    "var wire shield member via const receiver",
    ["var", C, "tune_shield", P, "shield", tune_ln, None] in vars_,
    "",
)
check("var wires only cross-file", all(w[1] != w[3] for w in vars_), "")

# 3. signal resolution: multi-script scene discriminates, unresolved counted
sigs = [w for w in mw if w[0] == "signal"]
check(
    "signal wires resolve in owning scripts",
    ["signal", M, "pressed", H, "_on_pressed", 0, None] in sigs
    and ["signal", M, "extra_ready", E, "_on_extra", 0, None] in sigs,
    str(sigs),
)
check(
    "signal counters",
    d1["meta"]["sig_resolved"] == 2 and d1["meta"]["sig_unresolved"] == 1,
    f"resolved={d1['meta'].get('sig_resolved')} "
    f"unresolved={d1['meta'].get('sig_unresolved')}",
)

# 4. fn roster complete for a sampled file: [name, line] in line order
want = sorted(
    ((f.name, f.line) for f in g.files["player.gd"].funcs.values()),
    key=lambda t: (t[1], t[0]),
)
check(
    "fns roster complete (player.gd)",
    [tuple(r) for r in d1["fns"].get("player.gd", [])] == want,
    f"got {d1['fns'].get('player.gd')}",
)
check(
    "fns covers every indexed file with funcs",
    set(d1["fns"]) == {p for p, fs in g.files.items() if fs.funcs and p in nodes},
    f"got {sorted(d1['fns'])}",
)

# 5. shape guards: 7-wide typed rows, deterministic sort, fedges/fio intact
check(
    "mwires rows 7-wide typed",
    all(len(w) == 7 and w[0] in ("call", "var", "signal") and w[6] is None for w in mw),
    "",
)
key = lambda w: (w[0], w[1], w[3], w[4], w[2], w[5])  # noqa: E731
check("mwires sorted", mw == sorted(mw, key=key), "")
check("fedges still 5-wide", all(len(fe) == 5 for fe in d1["fedges"]), "")
check(
    "fio shape intact",
    all(set(v) == {"sig", "ret", "w", "mp"} for v in d1["fio"].values()),
    "",
)

# 6. determinism: two builds identical modulo the generated_at stamp
for d in (d1, d2):
    d["meta"].pop("generated_at", None)
check(
    "two builds equal",
    json.dumps(d1, sort_keys=True) == json.dumps(d2, sort_keys=True),
    "",
)

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
