"""The server's FastMCP instance + tool annotations (issue #345 leaf).

One composition truth: server.py and every server_* family module
decorate handlers against THIS mcp instance — `import server` and
`from servercore import mcp` are the same object, so registration
happens exactly once per tool no matter who decorates. The version pin
(#207) and the MCP instructions (#237) ride with the instance because
they are construct-time facts, not boot lifecycle.
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from extractors import PRESETS


def _version() -> str:
    """The version serverInfo reports (issue #207): the neuronav package
    version — importlib.metadata answers for any installed copy (uvx/
    wheel; pyproject.toml is the version's source of truth), a plain
    checkout falls back to the pyproject beside this file (tomllib,
    stdlib since 3.11). Same derivation as onboard._uvx_ref, so
    serverInfo and the uvx tag pin (v<version>) can never disagree."""
    try:
        from importlib.metadata import PackageNotFoundError, version
        return version("neuronav")
    except PackageNotFoundError:
        import tomllib
        with open(Path(__file__).resolve().parent / "pyproject.toml", "rb") as fh:
            return tomllib.load(fh)["project"]["version"]


# Server instructions (MCP InitializeResult.instructions, issue #237):
# the agent-facing user manual — cross-tool workflow only, never an echo
# of tool descriptions (official guidance: concise, operational,
# model-agnostic; measured +25%% workflow adherence on GitHub's server).
_INSTRUCTIONS = (
    "Code-structure intelligence over the session's working directory. "
    "Workflow: call repo_map once per project for the layout; explore(topic) "
    "to orient on a subsystem; semantic_search / find_functions for lookups; "
    "search_text regex-greps the indexed files; context(path) is the "
    "one-file orientation dossier (cluster, neighbors, defines); "
    "clusters / crosstalk / arch_check map the subsystems themselves — "
    "communities, their coupling hotspots, project-rule violations — "
    "before a multi-file refactor; symbol_graph / dead_code / duplicates "
    "for structure questions; impact(symbol) for the pre-refactor "
    "blast-radius check — run it before renaming or removing anything; "
    "visualize opens the graph.html bake. A large first index build or "
    "bake never blocks silently (issue #315): progress flows as "
    "notifications/progress + stderr lines, the "
    "neuronav://onboarding/status resource is the pollable state, "
    "visualize answers 'queued' with the live phase, and reads served "
    "from a partially-built index are tagged stale: true. Tools are "
    "read-only except rescan (forces reindex) and memory "
    "(set/get/list/delete persistent project notes - save durable "
    "findings there, not transient state). "
    "Every tool takes an optional dir to target a different repo root. "
    "The index auto-refreshes on file drift; a tool marked 'degraded' "
    "still answers completely from the current index, though vector "
    "recall may be unavailable. An empty index answers with first-call "
    "guidance; onboard.py init --preset " + "|".join(PRESETS) + " "
    "scaffolds a config for unmatched file types."
)

mcp = FastMCP("neuronav", instructions=_INSTRUCTIONS)
# FastMCP forwards no version to its lowlevel Server (no such kwarg on
# mcp 1.29.x), and create_initialization_options then falls back to
# pkg_version("mcp") — serverInfo answered the mcp library's version,
# not neuronav's. Server.version is a plain attribute read at answer
# time, so pin it here (issue #207); an mcp that renames it fails
# loudly at import rather than silently misreporting.
mcp._mcp_server.version = _version()

# below except rescan and memory is pure read over the local index
READONLY = ToolAnnotations(readOnlyHint=True)
# The two mutators carry the rest of the annotation vocabulary
# (spec: the hints below are meaningful only when readOnlyHint is
# false, which is exactly the mutators). memory set overwrites and
# delete removes durable notes -> destructiveHint; rescan only
# rebuilds derived caches and converges -> additive-only +
# idempotent (issue #253).
MUTATING_MEMORY = ToolAnnotations(destructiveHint=True)
MUTATING_RESCAN = ToolAnnotations(destructiveHint=False,
                                  idempotentHint=True)


def _capped(names: list[str], cap: int) -> str:
    """First `cap` names, sorted by the caller, then an explicit +N more —
    the counts-everywhere discipline of explore's flow line (issue #125)."""
    shown = ", ".join(names[:cap])
    return shown + (f" +{len(names) - cap} more" if len(names) > cap else "")


def _capped_row(items: list, cap: int, render, indent: str = "  ") -> list[str]:
    """Row-wise sibling of _capped (issue #125): first `cap` items as
    rendered rows, then one explicit +N more line — the shared leaf for
    every hand-rolled row slice."""
    rows = [render(it) for it in items[:cap]]
    if len(items) > cap:
        rows.append(f"{indent}… +{len(items) - cap} more")
    return rows
