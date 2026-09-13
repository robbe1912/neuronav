"""onboard.py — one-command project onboarding (issue #27).

Kept OUT of nav.py on purpose: nav/server/graph are the always-loaded
core; setup/wiring runs once per project and must not weigh on the hot
path. Same pattern as explore.py (focused, self-contained).

  python onboard.py init  [--project PATH] [--index]
  python onboard.py wire  [--project PATH] [--index] [--omp [--omp-name NAME]]

- init: write ``<project>/.neuronav/config.json`` (walk-everything
  defaults, extensions = every registered extractor suffix,
  ``"state_dir": "default"`` opting into the project store — issue #91:
  a state_dir-less config aborts at load, the silent live-store default
  is gone) + idempotent ``.neuronav/`` line in the project's
  .gitignore. Never touches the neuronav install. Re-running init (or
  wire's init-if-missing) NEVER clobbers an existing config (issue
  #121) — the same existence guard as .neuroignore: an existing config
  is left byte-identical and the re-run notes it.
- wire: init if needed, then write/merge the project's ``.mcp.json``
  (and ``opencode.json`` when present) with NEURONAV_CONFIG pinned to
  the project-local config. Existing MCP jsons are read BOM-tolerant
  (utf-8-sig) and guarded: malformed content fails loud with a
  "fix or delete" message instead of a raw traceback, and writes go
  through a temp file + os.replace so a crash never leaves a truncated
  file over the user's wiring (issue #121). ``--omp`` additionally
  emits the same stdio entry into the omp harness user config
  (~/.omp/agent/mcp.json, issue #130) — ``NEURONAV_OMP_MCP`` overrides
  that path for portability/testing. The default server name is
  ``neuronav-<project>`` (derived from the project dir) so wiring
  several projects never collides in the single user file;
  ``--omp-name NAME`` overrides it. Cross-platform pure stdlib
  (replaces tools/wire-project.ps1).
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
import tempfile
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent


def scaffold(project: Path | None = None) -> Path:
    """Write the project-local config scaffold — no env/config switch.
    Returns the config path. Shared by init() and the universal mount's
    fresh-dir first contact (server.py, issue #131): one literal, so a
    scaffold written mid-call is byte-identical to `onboard.py init`'s.
    Idempotent on the config (issue #121): an existing config.json is
    left byte-identical — the same existence guard as .neuroignore, so a
    re-run never discards user customizations."""
    import nav
    from extractors import EXTENSIONS

    proj = (project or Path.cwd()).resolve()
    state = proj / ".neuronav"
    state.mkdir(parents=True, exist_ok=True)
    cfg_path = state / "config.json"
    if not cfg_path.is_file():
        cfg = {
            "root": str(proj),
            "collection": "main",
            "state_dir": "default",
            "include_dirs": list(nav.WALK_DEFAULTS["include_dirs"]),
            "extensions": sorted(EXTENSIONS),
            "exclude_dirs": list(nav.WALK_DEFAULTS["exclude_dirs"]),
        }
        cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8", newline="\n")
    else:
        print(f"config exists — left as-is: {cfg_path.as_posix()}")
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
    return cfg_path


def init(project: Path | None = None, index: bool = False) -> Path:
    """scaffold + switch this process (and children, via env) onto the
    new config. Returns the config path."""
    import nav

    cfg_path = scaffold(project)
    nav.use_config(cfg_path)
    if index:
        _index()
    return cfg_path


def _read_merge_json(path: Path, key_path: str) -> dict:
    """Read a project's user MCP json for merging. utf-8-sig so a
    leading BOM (PowerShell Set-Content legacy) parses; a malformed body,
    a non-object root, or a non-object merge container fails loud with a
    fix message instead of a raw traceback (issue #121)."""
    enc = "utf-8-sig"
    try:
        doc: dict = json.loads(path.read_text(encoding=enc))
    except json.JSONDecodeError as e:
        raise SystemExit(
            f"'{path}' is not valid JSON ({e}) — fix it or delete it so "
            "wire can manage the file"
        ) from e
    if not isinstance(doc, dict):
        raise SystemExit(
            f"'{path}' must be a JSON object (found {type(doc).__name__} at its "
            f"root) — fix it or delete it so wire can merge {key_path}"
        )
    container = doc.get(key_path)
    if container is not None and not isinstance(container, dict):
        raise SystemExit(
            f"'{path}': \"{key_path}\" must be a JSON object "
            f"(found {type(container).__name__}) — fix it or delete it so "
            "wire can merge the entry"
        )
    return doc


def _write_json_atomic(path: Path, doc: dict) -> None:
    """Write the updated json via a same-dir temp file + os.replace so a
    crash mid-write never truncates the user's wiring (issue #121)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(doc, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_OMP_MCP_ENV = "NEURONAV_OMP_MCP"


def wire(project: Path | None = None, index: bool = False,
         omp: bool = False, omp_name: str | None = None) -> Path:
    """init if needed, then write/merge project MCP entries. Returns the
    .mcp.json path; with ``omp`` the omp harness config path is emitted
    too. Never touches the install."""
    import nav

    proj = (project or Path.cwd()).resolve()
    cfg_path = proj / ".neuronav" / "config.json"
    if not cfg_path.is_file():
        init(proj, index=index)
    else:
        nav.use_config(cfg_path)
        if index:
            _index()
    entry = _entry(cfg_path)
    mcp_path = proj / ".mcp.json"
    doc = _read_merge_json(mcp_path, "mcpServers") if mcp_path.is_file() else {}
    doc.setdefault("mcpServers", {})["neuronav"] = entry
    _write_json_atomic(mcp_path, doc)
    oc_path = proj / "opencode.json"
    if oc_path.is_file():
        oc = _read_merge_json(oc_path, "mcp")
        oc.setdefault("mcp", {})["neuronav"] = {
            "type": "local",
            "command": [_entry(cfg_path)["command"], "-X", "utf8", str(TOOL_DIR / "server.py")],
            "enabled": True,
            "environment": {"NEURONAV_CONFIG": str(cfg_path)},
        }
        _write_json_atomic(oc_path, oc)
    if omp:
        _emit_omp(_omp_name(proj, omp_name), entry, _omp_mcp_path())
    return mcp_path


def _entry(cfg_path: Path) -> dict:
    """The shared stdio entry shape: repo venv python (falls back to the
    running interpreter), -X utf8 server.py, env pinning the project
    config. Identical in the project .mcp.json, opencode.json and the omp
    harness fragment."""
    venv = (TOOL_DIR / ".venv")
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    command = str(py) if py.is_file() else sys.executable
    return {
        "command": command,
        "args": ["-X", "utf8", str(TOOL_DIR / "server.py")],
        "env": {"NEURONAV_CONFIG": str(cfg_path)},
    }


def _omp_name(proj: Path, override: str | None) -> str:
    """Server name in the omp user config. Default is ``neuronav-<basename>``
    so wiring several projects never collides in the single file."""
    if override:
        return override
    return f"neuronav-{proj.name}"


def _omp_mcp_path() -> Path:
    """omp user-level mcpServers config (issue #130); NEURONAV_OMP_MCP
    overrides the path so tests stay hermetic."""
    env = os.environ.get(_OMP_MCP_ENV)
    return Path(env).expanduser() if env else Path.home() / ".omp" / "agent" / "mcp.json"


def _emit_omp(name: str, entry: dict, path: Path) -> None:
    """Write/merge the omp mcpServers fragment. User config merges by server
    name, so multiple projects coexist; other servers are preserved."""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc: dict = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    doc.setdefault("mcpServers", {})[name] = entry
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")



def _universal_entry() -> dict:
    """The universal-mount stdio entry (issue #131): same venv-python +
    server.py command as the project entry, but NO NEURONAV_CONFIG pin —
    every tool call routes per-call via its dir param, so one global
    entry per harness serves every repo."""
    e = _entry(Path())  # command/args shape only; the env pin is dropped
    e.pop("env", None)
    return e


_OPENCODE_MCP_ENV = "NEURONAV_OPENCODE_MCP"
_KILO_MCP_ENV = "NEURONAV_KILO_MCP"
_ZCODE_MCP_ENV = "NEURONAV_ZCODE_MCP"


def _opencode_user_path() -> Path:
    env = os.environ.get(_OPENCODE_MCP_ENV)
    return Path(env).expanduser() if env else Path.home() / ".config" / "opencode" / "opencode.json"


def _kilo_mcp_path() -> Path:
    """kilocode (VS Code ext) user MCP settings — Cline-family shape."""
    env = os.environ.get(_KILO_MCP_ENV)
    if env:
        return Path(env).expanduser()
    gs = Path.home() / "AppData" / "Roaming" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev"
    return gs / "mcp_settings.json"


def _zcode_mcp_path() -> Path:
    env = os.environ.get(_ZCODE_MCP_ENV)
    return Path(env).expanduser() if env else Path.home() / ".zcode" / "cli" / "config.json"


def global_wire(name: str = "neuronav") -> dict[str, Path]:
    """Emit ONE universal-mount entry (no config pin, per-call dir
    routing, issue #131) into every harness user config: omp
    (mcpServers), opencode (mcp), kilocode (mcpServers, Cline shape),
    zcode (mcp.servers). Merge-only by server name — existing entries
    (including per-project neuronav-<x> pins) are preserved untouched.
    Harness paths are env-overridable for hermetic tests. Returns the
    paths written. Idempotent: same entry bytes on re-run."""
    u = _universal_entry()
    written: dict[str, Path] = {}

    # omp: mcpServers.neuronav (merge with existing neuronav-<x> entries)
    omp = _omp_mcp_path()
    omp.parent.mkdir(parents=True, exist_ok=True)
    doc = json.loads(omp.read_text(encoding="utf-8-sig")) if omp.is_file() else {}
    doc.setdefault("mcpServers", {})[name] = u
    _write_json_atomic(omp, doc)
    written["omp"] = omp

    # opencode user: mcp.<name> — their local-server shape (command list)
    oc = _opencode_user_path()
    oc.parent.mkdir(parents=True, exist_ok=True)
    doc = _read_merge_json(oc, "mcp") if oc.is_file() else {}
    doc.setdefault("mcp", {})[name] = {
        "type": "local",
        "command": [u["command"], *u["args"]],
        "enabled": True,
    }
    _write_json_atomic(oc, doc)
    written["opencode"] = oc


    # kilocode: Cline-family mcpServers (stdio shape, autoApprove left to
    # the user — wiring never widens permissions)
    ki = _kilo_mcp_path()
    ki.parent.mkdir(parents=True, exist_ok=True)
    doc = _read_merge_json(ki, "mcpServers") if ki.is_file() else {}
    doc.setdefault("mcpServers", {})[name] = {
        "type": "stdio",
        "command": u["command"],
        "args": u["args"],
        "disabled": False,
    }
    _write_json_atomic(ki, doc)
    written["kilocode"] = ki

    # zcode: mcp.servers.<name> (local stdio shape)
    zc = _zcode_mcp_path()
    zc.parent.mkdir(parents=True, exist_ok=True)
    doc = _read_merge_json(zc, "mcp") if zc.is_file() else {}
    doc.setdefault("mcp", {}).setdefault("servers", {})[name] = {
        "type": "local",
        "command": u["command"],
        "args": u["args"],
    }
    _write_json_atomic(zc, doc)
    written["zcode"] = zc
    return written


def _index() -> None:
    """rescan + (optional) viz bake, with the add-on absent being fine."""
    import nav

    print(nav.rescan())
    try:
        import viz
    except ImportError:
        print("viz add-on not installed — skipped the graph.html bake")
        return
    print(viz.ensure_bake())


def open_viewer(project: Path | None = None) -> str:
    """Command to open a project's baked graph.html (issue #133).

    Production = open the self-contained bake directly (file://); serve.py
    is headless-dev only. The command differs per OS; cross-platform pure
    stdlib, no launching — just the hint.
    """
    bake = (project or Path.cwd()).resolve() / ".neuronav" / "graph.html"
    if sys.platform == "win32":
        # cmd `start` treats the FIRST quoted argument as a window title:
        # a space-y path needs the `start "" "path"` form
        if " " in str(bake):
            return f'start "" "{bake}"'
        return f"start {bake}"
    q = f'"{bake}"' if " " in str(bake) else str(bake)
    if sys.platform == "darwin":
        return f"open {q}"
    return f"xdg-open {q}"


if __name__ == "__main__":
    argv = list(sys.argv[1:])
    cmd = argv[0] if argv else "init"
    proj = None
    if "--project" in argv:
        i = argv.index("--project")
        proj = Path(argv[i + 1])
    do_index = "--index" in argv
    do_omp = "--omp" in argv
    omp_name = None
    if "--omp-name" in argv:
        i = argv.index("--omp-name")
        omp_name = argv[i + 1]
    target = (proj or Path.cwd()).resolve()
    if cmd == "init":
        p = init(proj, index=do_index)
        print(f"project config: {p}")
        print(f"state dir:      {target / '.neuronav'}")
        print(f"MCP wiring:     NEURONAV_CONFIG={p}")
        print("next:           onboard.py wire  (agents)  ·  nav.py rescan  ·  viz.py")
        if do_index and (target / ".neuronav" / "graph.html").is_file():
            print(f"viewer:         {open_viewer(target)}")
    elif cmd == "wire":
        m = wire(proj, index=do_index, omp=do_omp, omp_name=omp_name)
        print(f"mcp entry: {m}")
        if do_omp:
            print(f"omp fragment:   {_omp_mcp_path()} (server \"{_omp_name(target, omp_name)}\")")
            print("restart omp sessions (or /mcp reload) to pick it up")
        else:
            print("restart MCP client sessions in the project to pick it up")
            print("hint: add --omp to also emit an omp mcpServers fragment (issue #130)")
        if do_index and (target / ".neuronav" / "graph.html").is_file():
            print(f"viewer:         {open_viewer(target)}")
    elif cmd == "global-wire":
        for harness, path in global_wire().items():
            print(f"{harness:<9} {path}")
        print("universal entry \"neuronav\" (per-call dir routing, no config pin)")
        print("restart harness sessions to pick it up")
    else:
        print("usage: onboard.py [init|wire|global-wire] [--project PATH] [--index] [--omp [--omp-name NAME]]", file=sys.stderr)
        sys.exit(2)
