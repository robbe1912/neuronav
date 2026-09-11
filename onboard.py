"""onboard.py — one-command project onboarding (issue #27).

Kept OUT of nav.py on purpose: nav/server/graph are the always-loaded
core; setup/wiring runs once per project and must not weigh on the hot
path. Same pattern as explore.py (focused, self-contained).

  python onboard.py init  [--project PATH] [--index]
  python onboard.py wire  [--project PATH] [--index]

- init: write ``<project>/.neuronav/config.json`` (walk-everything
  defaults, extensions = every registered extractor suffix) + idempotent
  ``.neuronav/`` line in the project's .gitignore. Never touches the
  neuronav install.
- wire: init if needed, then write/merge the project's ``.mcp.json``
  (and ``opencode.json`` when present) with NEURONAV_CONFIG pinned to
  the project-local config. Cross-platform pure stdlib (replaces
  tools/wire-project.ps1).
- --index chains rescan (+ viz bake when the optional viz add-on is
  installed; skipped with a note otherwise).

Config discovery after init (implemented in nav._discover_config):
NEURONAV_CONFIG env -> <cwd>/.neuronav/config.json -> install-root
config.json only when cwd IS the checkout -> pure cwd defaults.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent


def init(project: Path | None = None, index: bool = False) -> Path:
    """Write the project-local config scaffold. Returns its path."""
    import nav
    from extractors import EXTENSIONS

    proj = (project or Path.cwd()).resolve()
    state = proj / ".neuronav"
    state.mkdir(parents=True, exist_ok=True)
    cfg_path = state / "config.json"
    cfg = {
        "root": str(proj),
        "collection": "main",
        "include_dirs": list(nav.WALK_DEFAULTS["include_dirs"]),
        "extensions": sorted(EXTENSIONS),
        "exclude_dirs": list(nav.WALK_DEFAULTS["exclude_dirs"]),
    }
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8", newline="\n")
    ig = state / ".neuroignore"
    if not ig.is_file():
        ig.write_text(
            "# neuronav ignores — one directory name per line (matched at any depth)\n"
            "# adjust freely: delete a line to re-include, add a name to exclude more\n"
            "# scratch conventions agent tooling drops into projects:\n"
            ".tmp\n"
            ".team_scratch\n",
            encoding="utf-8", newline="\n")
    gi = proj / ".gitignore"
    if gi.is_file():
        lines = gi.read_text(encoding="utf-8").splitlines()
        if ".neuronav/" not in lines:
            with open(gi, "a", encoding="utf-8", newline="\n") as f:
                f.write(".neuronav/\n" if lines and lines[-1] == "" else "\n.neuronav/\n")
    nav.use_config(cfg_path)
    if index:
        _index()
    return cfg_path


def wire(project: Path | None = None, index: bool = False) -> Path:
    """init if needed, then write/merge project MCP entries. Returns the
    .mcp.json path. Never touches the install."""
    import nav

    proj = (project or Path.cwd()).resolve()
    cfg_path = proj / ".neuronav" / "config.json"
    if not cfg_path.is_file():
        init(proj, index=index)
    else:
        nav.use_config(cfg_path)
        if index:
            _index()
    server = str(TOOL_DIR / "server.py")
    entry = {
        "command": sys.executable,
        "args": ["-X", "utf8", server],
        "env": {"NEURONAV_CONFIG": str(cfg_path)},
    }
    mcp_path = proj / ".mcp.json"
    doc: dict = json.loads(mcp_path.read_text(encoding="utf-8")) if mcp_path.is_file() else {}
    doc.setdefault("mcpServers", {})["neuronav"] = entry
    mcp_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")
    oc_path = proj / "opencode.json"
    if oc_path.is_file():
        oc: dict = json.loads(oc_path.read_text(encoding="utf-8"))
        oc.setdefault("mcp", {})["neuronav"] = {
            "type": "local",
            "command": [sys.executable, "-X", "utf8", server],
            "enabled": True,
            "environment": {"NEURONAV_CONFIG": str(cfg_path)},
        }
        oc_path.write_text(json.dumps(oc, indent=2) + "\n", encoding="utf-8", newline="\n")
    return mcp_path


def _index() -> None:
    """rescan + (optional) viz bake, with the add-on absent being fine."""
    import nav

    print(nav.rescan())
    try:
        import viz
    except ImportError:
        print("viz add-on not installed — skipped the graph.html bake")
        return
    print(viz.generate())
if __name__ == "__main__":
    argv = list(sys.argv[1:])
    cmd = argv[0] if argv else "init"
    proj = None
    if "--project" in argv:
        i = argv.index("--project")
        proj = Path(argv[i + 1])
    do_index = "--index" in argv
    target = (proj or Path.cwd()).resolve()
    if cmd == "init":
        p = init(proj, index=do_index)
        print(f"project config: {p}")
        print(f"state dir:      {target / '.neuronav'}")
        print(f"MCP wiring:     NEURONAV_CONFIG={p}")
        print("next:           onboard.py wire  (agents)  ·  nav.py rescan  ·  viz.py")
    elif cmd == "wire":
        m = wire(proj, index=do_index)
        print(f"mcp entry: {m}")
        print("restart MCP client sessions in the project to pick it up")
    else:
        print("usage: onboard.py [init|wire] [--project PATH] [--index]", file=sys.stderr)
        sys.exit(2)
