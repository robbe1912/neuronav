"""Frozen synthetic viz corpus — the hermetic data shape for test_viz (#100/#97/#146).

Builds "Helios", a fictional GDScript game repo whose ONLY purpose is to
give every test_viz interaction check the data shape it was authored
against: trunk corridors (file pairs with >=3 fn wires), a wired focus hub
with satellite fans and cross-subsystem pull (so the depth-3 lit set is
map-scale), fn-level cycles, a tight dead island, scenes with inst-dominant
hubs, resolved + unresolved signal connections, member-var wires, a tests
community with long inter-cluster links (highway arcs), and a real git
history so the churn channel has a hottest file.

Everything is fixed content — no rng, no wall-clock-dependent bytes. The
git history uses dates relative to build time so the 90-day churn window
never ages out, with fixed author/committer/message sequence so touch
counts (what churn normalizes) are byte-stable.

Hermetic by construction: the store lands under <dest>/.neuronav via the
scratch config written next to the tree; embeds are NEURONAV_EMBED_FAKE.
Never point this at a live store.

Usage (own process, per-command env only):
  python -X utf8 tests/vizcorpus_build.py --dest some/scratch/dir
  NEURONAV_CONFIG=<dest>/config.json NEURONAV_EMBED_FAKE=1 \\
      python -X utf8 tests/test_viz.py
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

CORPUS_TITLE = "Helios"
AUTHOR = "Helios Bot <helios@neuronav.invalid>"

# dirs fed to include_dirs (order = legend order; tests last so the
# tests-chip toggle has its own dir)
CODE_DIRS = [
    "core", "combat", "world", "ui", "audio", "net",
    "fx", "save_sys", "quests", "nav_ai", "legacy", "scenes", "tests",
]

# commit history: (day_offset, subject, touched dirs/files)
# touch counts: core/state.gd is touched by its feature commit + every
# hotfix -> the clear churn winner; everything else <= 2 touches.
FEATURE_COMMITS: list[tuple[int, str, list[str]]] = [
    (44, "core: run-state hub and autoloads", ["core"]),
    (40, "combat: rounds and statuses", ["combat"]),
    (36, "world: rooms and pickups", ["world"]),
    (32, "ui: hud wiring", ["ui"]),
    (28, "audio: mix bus", ["audio"]),
    (26, "net: session transport", ["net"]),
    (24, "fx: particle lanes", ["fx"]),
    (22, "save_sys: slot storage", ["save_sys"]),
    (20, "quests: objective graph", ["quests"]),
    (18, "nav_ai: steering", ["nav_ai"]),
    (17, "legacy: retired paths parked", ["legacy"]),
    (16, "scenes + tests scaffold", ["scenes", "tests"]),
]
HOTFIX_COMMITS: list[tuple[int, str, list[str]]] = [
    (12, "state: guard flag pushes", ["core/state.gd"]),
    (9, "state: cache invalidation", ["core/state.gd"]),
    (6, "state: session snapshot", ["core/state.gd"]),
    (4, "state + score: hot path", ["core/state.gd", "core/score.gd"]),
    (2, "state: logging polish", ["core/state.gd"]),
]

# leaf subsystem order — core satellites rotate through these so the
# depth-3 lit set reaches whole subsystems, not just the core island
LEAF_SYSTEMS = ["combat", "world", "ui", "audio", "net",
                "fx", "save_sys", "quests"]


def _hub_src(cls: str, doc: str) -> str:
    return f"""class_name {cls}
extends Node
# {CORPUS_TITLE} {doc} (synthetic corpus file).

var ticks: Node = null
var roster: Node = null

func setup() -> void:
\tticks = roster

func register(_who: Node) -> void:
\tpass

func step(_delta: float) -> void:
\tpass

func reset() -> void:
\tticks = null

func snapshot() -> Dictionary:
\treturn {{"ok": true}}

func status_of(_who: Node) -> int:
\treturn 0

func apply(_kind: Node, _turns: int) -> void:
\tpass

func clear(_kind: Node) -> void:
\tpass

func count() -> int:
\treturn 0
"""


def _sat_src(cls: str, hub_cls: str, hub_res: str, doc: str,
             calls: list[str], sib_cls: str, sib_res: str, sib_fn: str,
             next_sib_cls: str, next_sib_res: str, next_sib_fn: str,
             sig: str, sig_arg: str) -> str:
    call_lines = "\n".join(f"\th.{fn}()" for fn in calls)
    return f"""class_name {cls}
extends Node
# {CORPUS_TITLE} {doc} (synthetic corpus file).

const HubScene = preload("{hub_res}")
const SibScene = preload("{sib_res}")
const NextScene = preload("{next_sib_res}")

func _ready() -> void:
\tvar h: {hub_cls} = HubScene.new()
{call_lines}
\th.ticks = h.roster
\tvar snap: Dictionary = h.snapshot()
\tif snap.size() > 0:
\t\th.register(self)
\tSignalBus.{sig}.emit({sig_arg})

func hand_off() -> void:
\tvar s: {sib_cls} = SibScene.new()
\ts.{sib_fn}()

func cycle_back() -> void:
\tvar n: {next_sib_cls} = NextScene.new()
\tn.{next_sib_fn}()
"""


def _dead_src(cls: str, doc: str) -> str:
    return f"""class_name {cls}
extends Node
# {CORPUS_TITLE} {doc} (synthetic corpus file). Nothing references these
# funcs: they feed the dead tier.

func legacy_path() -> Node:
\treturn null

func old_tune(_v: int) -> void:
\tpass

func retired_hook(_what: Node) -> bool:
\treturn false

func stale_route(_from: Node, _to: Node) -> void:
\tpass
"""


def _state_src() -> str:
    return """class_name GameState
extends Node
# Helios run-state hub (autoload "State"). The corpus focus target: every
# core satellite calls into it, so its wired degree towers over the repo.

var flags: Node = null
var cache: Node = null
var session: Node = null

func push_flag(_name: String) -> void:
\tLogger.note("flag", _name)

func read_flag(_name: String) -> bool:
\treturn false

func drop_flag(_name: String) -> void:
\tpass

func bump_score(_delta: int) -> int:
\treturn 0

func score_total() -> int:
\treturn 0

func grant_item(_id: String) -> void:
\tpass

func item_count() -> int:
\treturn 0

func mark_quest(_id: String, _done: bool) -> void:
\tpass

func quest_done(_id: String) -> bool:
\treturn false

func open_session(_slot: int) -> void:
\tsession = null

func close_session() -> Dictionary:
\treturn {}

func snapshot_all() -> Dictionary:
\treturn {}

func reset_run() -> void:
\tflags = null
\tcache = null

func _ready() -> void:
\tLogger.boot("state")
\tSignalBus.run_saved.emit(0)
"""


def _state_sat_src(cls: str, doc: str, calls: list[str], sig: str,
                   sig_arg: str, foreign_mod: str,
                   foreign_fn: str) -> str:
    call_lines = "\n".join(f"\t{s}" for s in calls)
    fcls = "".join(p.capitalize() for p in foreign_mod.split("_")) + "Hub"
    return f"""class_name {cls}
extends Node
# {CORPUS_TITLE} {doc} (synthetic corpus file).

const StateScene = preload("res://core/state.gd")
const ForeignHub = preload("res://{foreign_mod}/{foreign_mod}_hub.gd")

func _ready() -> void:
\tvar s: GameState = StateScene.new()
{call_lines}
\ts.flags = s.cache
\tvar snap: Dictionary = s.snapshot_all()
\tif snap.size() > 0:
\t\ts.push_flag("warm")
\tvar f: {fcls} = ForeignHub.new()
\tf.{foreign_fn}()
\tSignalBus.{sig}.emit({sig_arg})
"""


def _signal_bus_src() -> str:
    return """class_name SignalBus
extends Node
# Helios autoload: the one place signals are declared.

signal combat_event(what)
signal score_changed(delta)
signal flag_set(fname)
signal run_saved(slot)
signal world_ready
"""


def _logger_src() -> str:
    return """class_name Logger
extends Node
# Helios autoload: tiny note-taker the state hub reports through.

var notes: Node = null

func note(_tag: String, _msg: String) -> void:
\tpass

func boot(_what: String) -> void:
\tnotes = null

func flush() -> Dictionary:
\treturn {}
"""


def _hud_src() -> str:
    return """class_name Hud
extends CanvasLayer
# Helios HUD: binds the signal bus + pushes score through the state hub.

const StateScene = preload("res://core/state.gd")

func _ready() -> void:
\tSignalBus.score_changed.connect(_on_score)
\tSignalBus.flag_set.connect(_on_flag)

func _on_score(_delta: int) -> void:
\tvar s: GameState = StateScene.new()
\ts.bump_score(_delta)
\ts.flags = s.cache

func _on_flag(_fname: String) -> void:
\tvar s: GameState = StateScene.new()
\ts.push_flag(_fname)

func _on_button_pressed() -> void:
\tvar s: GameState = StateScene.new()
\ts.reset_run()
"""


def _title_src() -> str:
    return """class_name TitleScreen
extends Control
# Helios title (dead scene): nothing instances this script's funcs.

func spin_logo() -> void:
\tpass

func fade_in(_seconds: float) -> void:
\tpass
"""


def _main_scene_src() -> str:
    return """class_name MainRun
extends Node2D
# Helios entry scene script: wires the run together through the state hub.

const StateScene = preload("res://core/state.gd")

func _ready() -> void:
\tvar s: GameState = StateScene.new()
\ts.open_session(1)
\ts.push_flag("boot")
\tSignalBus.world_ready.emit()
\tSignalBus.score_changed.connect(_on_score)

func _on_score(_delta: int) -> void:
\tvar s: GameState = StateScene.new()
\ts.score_total()
"""


def _enemy_src() -> str:
    return """class_name Enemy
extends CharacterBody2D
# Helios enemy body: leans on the combat hub.

const HubScene = preload("res://combat/combat_hub.gd")

func _ready() -> void:
\tvar h: CombatHub = HubScene.new()
\th.setup()
\th.apply(null, 1)
\th.ticks = h.roster

func take_hit(_amount: int) -> void:
\tvar h: CombatHub = HubScene.new()
\th.status_of(self)
"""


def _pickup_src() -> str:
    return """class_name Pickup
extends Area2D
# Helios pickup: reports through the world hub.

const HubScene = preload("res://world/world_hub.gd")

func _ready() -> void:
\tvar h: WorldHub = HubScene.new()
\th.setup()
\th.register(self)

func collect() -> void:
\tvar h: WorldHub = HubScene.new()
\th.count()
\th.clear(null)
"""


def _test_src(mods: list[tuple[str, list[str]]], stem: str) -> str:
    """One test file touching SEVERAL subsystems — the tests community
    shares no dominant neighbor, so it clusters on its own dir seed and
    its links stay long + inter-cluster (highway arcs). Locals are
    TYPED against the hub class so the calls resolve to real file edges
    (untyped preload-new vars leave the tests island linkless)."""
    def _cls(m: str) -> str:
        return "".join(p.capitalize() for p in m.split("_")) + "Hub"
    preloads = "\n".join(
        f'const Hub{ix} = preload("res://{m}/{m}_hub.gd")'
        for ix, (m, _c) in enumerate(mods))
    body = []
    for ix, (m, calls) in enumerate(mods):
        call_lines = "\n".join(f"\th.{fn}()" for fn in calls)
        body.append(f"func test_{m}_{stem}() -> void:\n"
                    f"\tvar h: {_cls(m)} = Hub{ix}.new()\n{call_lines}\n"
                    f"\tassert(h.snapshot().size() >= 0)")
    return ("extends Node\n"
            f"# {CORPUS_TITLE} test: {stem} behaviour (synthetic corpus file).\n\n"
            + preloads + "\n\nfunc _ready() -> void:\n"
            + "".join(f"\ttest_{m}_{stem}()\n" for m, _c in mods)
            + "\n" + "\n\n".join(body) + "\n")


def _tscn(name: str, nodes: list[str], connections: list[str] | None = None,
          scripts: list[tuple[str, str]] | None = None) -> str:
    scripts = scripts or []
    load_steps = 1 + len(scripts)
    out = [f"[gd_scene load_steps={load_steps} format=3]"]
    for i, (path, _n) in enumerate(scripts, 1):
        out.append(f'[ext_resource type="Script" path="{path}" id="{i}"]')
    out.append(f'[node name="{name}" type="Node2D"]')
    out.extend(nodes)
    out.extend(connections or [])
    return "\n".join(out) + "\n"


def _instanced_tscn(name: str, instances: list[str]) -> str:
    load_steps = 1 + len(instances)
    out = [f"[gd_scene load_steps={load_steps} format=3]"]
    for i, path in enumerate(instances, 1):
        out.append(f'[ext_resource type="PackedScene" path="{path}" id="{i}"]')
    out.append(f'[node name="{name}" type="Node2D"]')
    for i in range(1, len(instances) + 1):
        out.append(f'[node name="Inst{i}" parent="." instance=ExtResource("{i}")]')
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# corpus assembly


def build_files() -> dict[str, str]:
    """rel path (posix) -> final content. Fixed order, fixed bytes."""
    f: dict[str, str] = {}

    # -- core: the focus subsystem --------------------------------------
    f["core/state.gd"] = _state_src()
    f["core/signal_bus.gd"] = _signal_bus_src()
    f["core/logger.gd"] = _logger_src()
    core_sats = {
        "score": ["s.bump_score(2)", "s.score_total()", "s.snapshot_all()",
                  "s.push_flag(\"score\")", "s.reset_run()"],
        "inventory": ["s.grant_item(\"key\")", "s.item_count()",
                      "s.drop_flag(\"key\")", "s.snapshot_all()",
                      "s.read_flag(\"key\")"],
        "flags": ["s.push_flag(\"alpha\")", "s.read_flag(\"alpha\")",
                  "s.drop_flag(\"alpha\")", "s.snapshot_all()",
                  "s.bump_score(1)"],
        "stats": ["s.score_total()", "s.item_count()", "s.quest_done(\"q1\")",
                  "s.snapshot_all()", "s.open_session(3)"],
        "badges": ["s.mark_quest(\"b1\", true)", "s.quest_done(\"b1\")",
                   "s.score_total()", "s.snapshot_all()",
                   "s.item_count()"],
        "progress": ["s.open_session(2)", "s.close_session()",
                     "s.snapshot_all()", "s.reset_run()",
                     "s.push_flag(\"mid\")"],
        "save": ["s.open_session(0)", "s.close_session()",
                 "s.snapshot_all()", "s.push_flag(\"slot\")",
                 "s.drop_flag(\"tmp\")"],
        "settings": ["s.read_flag(\"widescreen\")", "s.snapshot_all()",
                     "s.score_total()", "s.push_flag(\"cfg\")",
                     "s.reset_run()"],
    }
    for i, (nm, calls) in enumerate(core_sats.items()):
        cls = "".join(p.capitalize() for p in nm.split("_"))
        sig = ["score_changed", "flag_set", "run_saved", "combat_event"][i % 4]
        arg = ["2", "\"warm\"", "0", "\"core\""][i % 4]
        foreign = LEAF_SYSTEMS[i % len(LEAF_SYSTEMS)]
        foreign_fn = ["setup", "step", "snapshot", "register"][i % 4]
        f[f"core/{nm}.gd"] = _state_sat_src(cls, f"core {nm} keeper", calls,
                                            sig, arg, foreign, foreign_fn)

    # -- leaf subsystems: hub + 3 satellites -----------------------------
    sys_meta = {
        "combat": "rounds and statuses",
        "world": "rooms and pickups",
        "ui": "chrome wiring",
        "audio": "mix bus",
        "net": "session transport",
        "fx": "particle lanes",
        "save_sys": "slot storage",
        "quests": "objective graph",
        "nav_ai": "steering",
    }
    for mod, doc in sys_meta.items():
        hub_cls = "".join(p.capitalize() for p in mod.split("_")) + "Hub"
        f[f"{mod}/{mod}_hub.gd"] = _hub_src(hub_cls, doc)
        sats = ["a", "b", "c"]
        cls_by_sfx = {s: f"{hub_cls[:-3]}{s.upper()}" for s in sats}
        for i, sfx in enumerate(sats):
            calls = [["setup", "register", "apply", "snapshot"],
                     ["step", "status_of", "count", "snapshot"],
                     ["reset", "clear", "snapshot", "count"]][i]
            sig = ["combat_event", "score_changed", "flag_set"][i]
            arg = ["\"ready\"", "1", "\"warm\""][i]
            # fn-level cycle a -> b -> c -> a (feeds SCCs / cycles lens)
            sib, sib_fn = sats[(i + 1) % 3], "hand_off"
            nxt, nxt_fn = sats[(i + 2) % 3], "cycle_back"
            f[f"{mod}/{mod}_{sfx}.gd"] = _sat_src(
                cls_by_sfx[sfx], hub_cls, f"res://{mod}/{mod}_hub.gd",
                f"{mod} {sfx}", calls,
                cls_by_sfx[sib], f"res://{mod}/{mod}_{sib}.gd", sib_fn,
                cls_by_sfx[nxt], f"res://{mod}/{mod}_{nxt}.gd", nxt_fn,
                sig, arg)

    # scene-script satellites (combat/world already have 4 files each)
    f["combat/enemy.gd"] = _enemy_src()
    f["world/pickup.gd"] = _pickup_src()

    # -- ui extras: hud (signal wiring) -----------------------------------
    f["ui/hud.gd"] = _hud_src()

    # -- legacy: the dead island (own cluster, no links anywhere) --------
    for i in range(10):
        cls = f"Legacy{i:02d}"
        f[f"legacy/legacy_{i:02d}.gd"] = _dead_src(cls, "retired path")

    # -- scenes ----------------------------------------------------------
    f["legacy/title.tscn"] = _tscn(  # dead scene (kept with its script)
        "Title", ['[node name="Logo" type="TextureRect" parent="."]'],
        scripts=[("res://legacy/title.gd", "Title")])
    f["scenes/main.gd"] = _main_scene_src()
    f["scenes/main.tscn"] = _instanced_tscn(
        "Main", ["res://scenes/hud.tscn", "res://scenes/menu.tscn",
                 "res://scenes/world.tscn", "res://scenes/fx_layer.tscn"])
    f["scenes/hud.tscn"] = _tscn(
        "HUD",
        ['[node name="ScoreLabel" type="Label" parent="."]',
         '[node name="Button" type="Button" parent="."]'],
        ['[connection signal="pressed" from="Button" to="." method="_on_button_pressed"]',
         '[connection signal="pressed" from="ScoreLabel" to="." method="_on_missing"]'],
        scripts=[("res://ui/hud.gd", "HUD")])
    f["scenes/menu.tscn"] = _tscn(
        "Menu",
        ['[node name="VBox" type="VBoxContainer" parent="."]'])
    f["scenes/world.tscn"] = _instanced_tscn(
        "World", ["res://scenes/enemy.tscn", "res://scenes/pickup.tscn"])
    f["scenes/enemy.tscn"] = _tscn(
        "Enemy", ['[node name="Sprite" type="Sprite2D" parent="."]'],
        scripts=[("res://combat/enemy.gd", "Enemy")])
    f["scenes/pickup.tscn"] = _tscn(
        "Pickup", ['[node name="Shape" type="CollisionShape2D" parent="."]'],
        scripts=[("res://world/pickup.gd", "Pickup")])
    f["scenes/fx_layer.tscn"] = _tscn(
        "FxLayer", ['[node name="Particles" type="GPUParticles2D" parent="."]'])

    # -- tests community: each file spans 3-5 subsystems so the tests
    # dir keeps its OWN community centroid (not any single hub's) —
    # tests->hub links stay inter-cluster AND long, which is what makes
    # them highway arcs (the hidden-test arcs check reads hw slots) ----
    f["tests/test_rounds.gd"] = _test_src(
        [("combat", ["setup", "register", "snapshot", "count"]),
         ("net", ["setup", "step", "snapshot"]),
         ("audio", ["count", "snapshot"]),
         ("save_sys", ["reset", "snapshot"])], "rounds")
    f["tests/test_rooms.gd"] = _test_src(
        [("world", ["setup", "register", "step", "snapshot"]),
         ("fx", ["reset", "clear", "snapshot"]),
         ("quests", ["step", "count"]),
         ("ui", ["register", "snapshot"])], "rooms")
    f["tests/test_state.gd"] = _test_src(
        [("audio", ["setup", "snapshot"]),
         ("quests", ["step", "count", "snapshot"]),
         ("save_sys", ["reset", "snapshot"]),
         ("net", ["status_of", "snapshot"]),
         ("combat", ["apply", "snapshot"])], "keepers")
    f["tests/test_routes.gd"] = _test_src(
        [("nav_ai", ["setup", "step", "snapshot"]),
         ("ui", ["register", "snapshot"]),
         ("fx", ["clear", "count"]),
         ("world", ["status_of", "snapshot"])], "routes")
    # -- project.godot (autoloads -> entry roots -> strata depths) --------
    f["project.godot"] = (
        "; Helios project file (synthetic corpus).\n"
        "[autoload]\n"
        'State="*res://core/state.gd"\n'
        'SignalBus="*res://core/signal_bus.gd"\n'
        'Logger="*res://core/logger.gd"\n'
        "\n[display]\n"
        "window/size/viewport_width=1280\n"
        "window/size/viewport_height=720\n"
    )
    return f


def _git(dest: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "core.autocrlf=false", *args],
        cwd=dest, check=check, capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": AUTHOR.split(" <")[0],
             "GIT_AUTHOR_EMAIL": AUTHOR.split("<")[1].rstrip(">"),
             "GIT_COMMITTER_NAME": AUTHOR.split(" <")[0],
             "GIT_COMMITTER_EMAIL": AUTHOR.split("<")[1].rstrip(">")})


def build_history(dest: Path, files: dict[str, str], base: _dt.datetime) -> None:
    """Deterministic commit sequence; touch counts feed the churn channel."""
    # a scratch repo of our own, ALWAYS — if git ever walks up to a parent
    # repo the commits (and their file adds) would land in someone else's
    # history. Init, then prove the repo root is exactly `dest`.
    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "config", "user.name", AUTHOR.split(" <")[0])
    _git(dest, "config", "user.email", AUTHOR.split("<")[1].rstrip(">"))
    if not (dest / ".git").is_dir():
        raise SystemExit(f"corpus repo init failed: no {dest / '.git'}")

    def commit(day: int, subject: str) -> None:
        ts = base + _dt.timedelta(days=day)
        iso = ts.strftime("%Y-%m-%dT%H:%M:%S")
        name, email = AUTHOR.split(" <")[0], AUTHOR.split("<")[1].rstrip(">")
        env = {**os.environ,
               "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
               "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
               "GIT_AUTHOR_DATE": iso, "GIT_COMMITTER_DATE": iso}
        if not (dest / ".git").is_dir():
            raise SystemExit("corpus .git vanished — refusing to let git "
                             "walk up into a parent repo")
        for stage in ("add", "commit"):
            argv = (["git", "-c", "core.autocrlf=false", "add", "-A"] if stage == "add"
                    else ["git", "-c", "core.autocrlf=false", "commit", "-q",
                          "--allow-empty", "-m", subject])
            subprocess.run(argv, cwd=dest, check=True, capture_output=True,
                           text=True, env=env)

    # schedule: scaffold + features + hotfixes, each touch a REAL content
    # change (a deterministic per-revision footer comment) — git counts a
    # touch only when the blob changes, so identical rewrites would leave
    # every file at one touch and the churn channel flat.
    all_rels = sorted(files)
    schedule: list[tuple[int, str, list[str]]] = [
        (45, "helios: initial scaffold", all_rels)]
    for day, subject, dirs in FEATURE_COMMITS:
        schedule.append((day, subject, [
            r for r in all_rels
            if any(r.startswith(d + "/") for d in dirs)]))
    for day, subject, rels in HOTFIX_COMMITS:
        schedule.append((day, subject, list(rels)))

    def variant(rel: str, n: int) -> str:
        cmt = ("; corpus rev %d" if rel.endswith((".tscn", ".godot"))
               else "# corpus rev %d") % n
        return files[rel].rstrip("\n") + f"\n{cmt}\n"

    rev = dict.fromkeys(all_rels, 0)
    for day, subject, rels in schedule:
        for rel in rels:
            rev[rel] += 1
            p = dest / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(variant(rel, rev[rel]), encoding="utf-8",
                         newline="\n")
        commit(day, subject)


def write_config(dest: Path) -> Path:
    cfg = dest / "config.json"
    cfg.write_text(json.dumps({
        "root": str(dest),
        "collection": "vizcorpus",
        "include_dirs": CODE_DIRS,
        "extensions": [".gd", ".tscn"],
        "exclude_dirs": [".git"],
        "state_dir": "default",
    }, indent=2) + "\n", encoding="utf-8", newline="\n")
    return cfg


def build_store(cfg: Path) -> str:
    """Rescan + bake inside a child process so this script never binds a
    nav config itself (per-command env only)."""
    code = (
        "import sys, nav, viz\n"
        "nav.rescan()\n"
        "p = viz.generate()\n"
        "print('CORPUS files=%d' % nav.count())\n"
        "print('BAKE ' + str(p))\n"
    )
    env = {**os.environ,
           "NEURONAV_CONFIG": str(cfg),
           "NEURONAV_EMBED_FAKE": "1",
           "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run([sys.executable, "-X", "utf8", "-c", code],
                       cwd=str(REPO), env=env, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stdout + r.stderr)
        raise SystemExit("corpus store build failed")
    return r.stdout.strip()


def _force_rmtree(path: Path) -> None:
    import shutil
    import stat

    def _onexc(fn, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        fn(p)

    shutil.rmtree(path, onexc=_onexc)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dest", required=True,
                    help="scratch dir for the corpus repo + store")
    args = ap.parse_args()

    dest = Path(args.dest).resolve()
    if dest.exists() and any(dest.iterdir()):
        _force_rmtree(dest)
    dest.mkdir(parents=True)

    files = build_files()
    build_history(dest, files, _dt.datetime.now() - _dt.timedelta(hours=12))
    cfg = write_config(dest)
    out = build_store(cfg)
    for line in out.splitlines():
        print(line)
    html = dest / ".neuronav" / "graph.html"
    size = html.stat().st_size if html.is_file() else -1
    print(f"CORPUS READY dest={dest} graph.html={size}B")
    print(f"gate: NEURONAV_CONFIG={cfg} python -X utf8 tests/test_viz.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
