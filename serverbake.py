"""Background bake leaf (issue #360): the bake queue — _BAKE_* state,
_bake_loop/_start_baker, the visualize tool and the neuronav://onboarding/
status resource — moved verbatim out of server.py's lifecycle core (both
#354 P1 bugs of the review wave shipped inside exactly this block; the
seam that motivated the carve).
server.py owns every rail this block reads and imports THIS module (the
reverse import would cycle), so register() receives the server module —
same composition seam as the query families (issue #345) — and binds
live rail references into this module's dict, per shape. Call rails
(_route/_serve/_progress_set/_progress_line) ride late proxies that
resolve the owner's current attribute at call time (the servercore._Rail
law); object/value rails (_SCOPE_LOCK, the _BOOT_* gate, LOCK_WAIT_S)
bind to the owner's current object and are re-read on every proxy call
and status read, because a PEP 562 module __getattr__ cannot do this —
it fires on attribute access only, never on the LOAD_GLOBAL misses
inside these verbatim bodies, and a value frozen at import would hide
the pinning suites' rebinds on server (LOCK_WAIT_S in test_autorescan;
_BOOT_READY/_BOOT_THREAD/_BOOT_FATAL in test_onboardprogress) — the
#359 no-copy law.

Registration is composition and stays in server.py (issue #345):
visualize's mcp.tool decoration runs there after the query families, so
tools/list is byte-identical; only the onboarding-status resource
decorates here. register() returns the handlers + state singletons so
server.py keeps binding them at module scope — the suites reach
server.visualize / server._onboarding_status / server._BAKE_STATE /
server._BAKE_LOCK where they always did. _BAKE_THREAD stays local to
this module: _start_baker's `global` rebind owns it here, and a frozen
server-side alias would go stale the first bake."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from mcp.server.fastmcp import Context
from servercore import mcp

import navconfig

# ---- background bake (issue #315) ------------------------------------------
# viz.ensure_bake() on a large store outlives any client timeout; run it on
# a dedicated baker thread fed by a FIFO of store configs (None = the boot
# store). The boot-store bake waits for _BOOT_READY first (baking a
# half-built index wastes minutes); a foreign dir bakes under the scope
# lock + config_scope because nav's globals are shared routing state.
_BAKE_LOCK = threading.Lock()
_BAKE_STATE: dict = {"running": False, "started": 0.0, "error": "", "done_at": 0.0, "out": ""}
_BAKE_QUEUE: list[Path | None] = []
_BAKE_WAKE = threading.Event()
_BAKE_START_LOCK = threading.Lock()
_BAKE_THREAD: threading.Thread | None = None


def _bake_loop() -> None:
    while True:
        _BAKE_WAKE.wait()
        _BAKE_WAKE.clear()
        while _BAKE_QUEUE:
            with _BAKE_LOCK:
                target = _BAKE_QUEUE.pop(0)
                _BAKE_STATE.update(running=True, started=time.monotonic(), error="")
            t0 = time.monotonic()
            note = target.as_posix() if target else "the boot store"
            _progress_set("bake", note=f"graph.html bake — {note}")
            try:
                import viz

                # Both arms hold _SCOPE_LOCK (PR #318 gate, GK P1 race): a
                # bake reads nav globals (STATE_DIR, its one chroma fetch)
                # minutes deep in _build_data — a concurrent dir-routed call
                # would swap them mid-bake and mix stores. RLock, no
                # deadlock: the boot gate opened before the lock was
                # released (READY is set after the boot sequence drops it)
                # and routed bodies release on exit — the bake just
                # serializes like any other scoped op.
                if target is None:
                    # boot store — issue #354: the arm is picked by TARGET,
                    # never by _BOOT_THREAD (that handle is never cleared
                    # after boot, so the old gate routed every live-server
                    # foreign bake here, baking the BOOT store while the ack
                    # named the foreign dir). Bake only once the boot gate
                    # opened (a half-built index wastes minutes);
                    # in-process imports have no boot thread (pre-#273
                    # semantics) — the gate is already open for them, and
                    # the bounded wait fails loud instead of stalling the
                    # queue behind a wedged boot.
                    if _BOOT_THREAD is not None:
                        wait_s = _BOOT_WAIT_S + LOCK_WAIT_S + 60.0
                        if not _BOOT_READY.wait(wait_s):
                            raise TimeoutError(
                                f"neuronav: boot still incomplete after "
                                f"{wait_s:g}s — bake aborted; see the "
                                "neuronav: stderr lines"
                            )
                    with _SCOPE_LOCK:
                        out = viz.ensure_bake()
                else:
                    # foreign store: swap nav's globals for the bake only
                    with _SCOPE_LOCK:
                        with navconfig.config_scope(target):
                            out = viz.ensure_bake()
                with _BAKE_LOCK:
                    _BAKE_STATE.update(
                        running=False, done_at=time.monotonic(), out=str(out), error=""
                    )
                print(
                    f"neuronav: bake complete: {out} ({time.monotonic() - t0:.1f}s)",
                    file=sys.stderr,
                )
            except Exception as e:  # loud-failures law: a dead bake says so
                with _BAKE_LOCK:
                    _BAKE_STATE.update(
                        running=False, done_at=time.monotonic(),
                        error=f"{type(e).__name__}: {e}",
                    )
                print(f"neuronav: bake FAILED: {e!r}", file=sys.stderr)
            finally:
                _progress_set("done", note=f"bake finished — {note}")


def _start_baker() -> None:
    global _BAKE_THREAD
    with _BAKE_START_LOCK:
        if _BAKE_THREAD is None or not _BAKE_THREAD.is_alive():
            _BAKE_THREAD = threading.Thread(
                target=_bake_loop, name="neuronav-bake", daemon=True
            )
            _BAKE_THREAD.start()


async def visualize(dir: str = "", ctx: Context = None) -> str:
    """Generate the interactive 3D code-graph (rotatable neuron map).

    Nodes = files (colored by subsystem cluster, red-tinted when they contain
    dead-code candidates), edges = calls/instancing/signals. Search box,
    cluster filter chips, dead-code toggle, click for connections.
    Returns the bake path + its openable file:// URI — the file is fully
    self-contained and boots directly in a browser (issue #133). Regenerate
    after rescan if the graph changed materially.

    Issue #315: a bake on a large store outlives the client's timeout, so
    the tool no longer blocks on it. It validates, queues the bake on the
    background baker, and answers immediately with current build/bake
    progress; the bake result (path) and any failure surface on stderr and
    the neuronav://onboarding/status resource.

    dir="" serves the boot config's repo; any other path routes the call
    to that checkout (issue #131) through the same gate as the read tools:
    a fresh dir onboards in-call (scaffold + first-contact index build,
    progress on the usual channels) and the build summary rides above the
    ack; a warm dir heals drift and acks immediately. The queued bake then
    lands in THAT checkout's store, never the boot one (issue #354).
    """
    def _body() -> str:
        try:
            import viz  # noqa: F401 — delete-able-surface guard (unchanged)
        except ImportError:
            return ("viz add-on not installed — delete-able surface is viz.py + vendor/ + "
                    "tools/serve.py; core tools (search/repo_map/context/...) work without it. "
                    "Restore viz.py to re-enable the bake.")
        if dir:
            # issue #354: the routed arm rides the SAME _route gate as the
            # read tools (boot gate, scaffold/validate, config_scope, first
            # contact) — a fresh dir builds its index inside this call, so
            # the queued bake finds a served store instead of the #64
            # guard's empty-store refusal; the build summary rides above
            # the ack like memory's does.
            with _route(dir) as prelude:
                resolved = Path(dir).expanduser().resolve()
                target = resolved / ".neuronav" / "config.json"
        else:
            prelude = None
            target = None  # the boot config's store
        with _BAKE_LOCK:
            _BAKE_QUEUE.append(target)
            in_flight = _BAKE_STATE["running"]
            ahead = len(_BAKE_QUEUE) - 1
        _BAKE_WAKE.set()
        _start_baker()
        line = _progress_line() or "no build in flight"
        where = "bake running" if in_flight and ahead == 0 else (
            f"queued behind {ahead} bake(s)" if ahead or in_flight else "starting now"
        )
        ack = (
            f"bake accepted — {where}; build state: {line}. The bake runs in "
            "the background (minutes on a large store): watch stderr or poll "
            "the neuronav://onboarding/status resource — graph.html lands at "
            "the store's .neuronav when done, and a failure there is loud."
        )
        return f"{prelude}\n{ack}" if prelude else ack

    return await _serve(_body, ctx)


@mcp.resource("neuronav://onboarding/status")
def _onboarding_status() -> str:
    """Pollable build/bake state (issue #315) — the liveness channel that
    answers while tools are gated on a first index build or a bake. Reads
    two dicts under their locks; never touches nav, so it cannot block on
    the store or the scope lock."""
    lines = []
    if _BOOT_FATAL is not None:
        lines.append(f"boot: FATAL — {_BOOT_FATAL}")
    elif _BOOT_THREAD is not None and not _BOOT_READY.is_set():
        lines.append(f"boot: building — {_progress_line() or 'starting'}")
    else:
        lines.append("boot: ready")
    line = _progress_line()
    if line:
        lines.append(f"build: {line}")
    with _BAKE_LOCK:
        running = _BAKE_STATE["running"]
        started = _BAKE_STATE["started"]
        error = _BAKE_STATE["error"]
        done_at = _BAKE_STATE["done_at"]
        out = _BAKE_STATE["out"]
        queued = list(_BAKE_QUEUE)
    if running:
        lines.append(f"bake: running ({time.monotonic() - started:.0f}s in)")
    elif error:
        lines.append(f"bake: FAILED — {error}")
    elif out:
        lines.append(f"bake: done {time.monotonic() - done_at:.0f}s ago — {out}")
    else:
        lines.append("bake: not run this session")
    if queued:
        lines.append(
            "bake queue: " + ", ".join(q.as_posix() if q else "boot store" for q in queued)
        )
    return "\n".join(lines)

# ---- rail resolution (issue #359 law) ---------------------------------------
# The server-owned names above are NOT defined in the verbatim block, and
# they cannot be: a PEP 562 module __getattr__ only fires on ATTRIBUTE
# access (serverbake._serve), never on the LOAD_GLOBAL misses inside these
# bodies — moved code reads its own module globals. register() therefore
# writes live bindings into this module's dict, per shape:
#
#   * call rails (_route/_serve/_progress_set/_progress_line) bind as
#     late proxies that resolve the CURRENT owner attribute at call time
#     — the servercore._Rail law, so test rebinds on server's namespace
#     reach the bake path exactly as they reach the query families;
#   * object/value rails (_SCOPE_LOCK, _BOOT_FATAL, _BOOT_THREAD,
#     _BOOT_READY, _BOOT_WAIT_S, LOCK_WAIT_S) bind to the owner's current
#     object/value and are RE-READ from the owner on every proxy call and
#     at every status read (fresh_bind) — the pinning suites rebind
#     several of these on server (LOCK_WAIT_S=0.5 in test_autorescan;
#     _BOOT_FATAL/_BOOT_THREAD/_BOOT_READY.clear() in test_onboardprogress),
#     and a value frozen at import would hide those rebinds (#359
#     copy-binding law, object shape).
#
# The bake loop's volatile reads (_BOOT_THREAD is not None, the
# _BOOT_WAIT_S + LOCK_WAIT_S sum, _BOOT_READY.wait) all sit AFTER the
# _progress_set call that opens each queue item, so the per-call
# fresh_bind lands them on current values; _onboarding_status reads
# _BOOT_FATAL before any rail call, so register() hands server a thin
# wrapper that fresh-binds first. Those wrappers are the only non-
# verbatim code below, and they do nothing but re-read names.
_OWNER = None
_CALL_RAILS = ("_route", "_serve", "_progress_set", "_progress_line")
_VALUE_RAILS = (
    "_SCOPE_LOCK", "_BOOT_FATAL", "_BOOT_THREAD", "_BOOT_READY",
    "_BOOT_WAIT_S", "LOCK_WAIT_S",
)


class _RailProxy:
    """Late-binding call rail: fresh-bind the value rails, then resolve
    and call the owner's current attribute (servercore._Rail's law,
    plus the refresh that keeps the value rails live)."""

    __slots__ = ("_owner", "_name")

    def __init__(self, owner, name):
        self._owner = owner
        self._name = name

    def __call__(self, *args, **kwargs):
        _fresh_bind()
        return getattr(self._owner, self._name)(*args, **kwargs)


def _fresh_bind() -> None:
    """Re-read the owner's object/value rails into this module's dict."""
    g = globals()
    for name in _VALUE_RAILS:
        g[name] = getattr(_OWNER, name)


def register(server_mod):
    """Compose this leaf onto the server (issue #345 seam): server.py passes
    sys.modules[__name__] — the true rail owner in both launch shapes
    (python server.py runs it as __main__; the console script imports it
    as server) — and binds the returned handlers + state singletons at
    module scope, keeping every historical server.<name> reach (the
    suites call server.visualize / server._onboarding_status and read
    server._BAKE_STATE / server._BAKE_LOCK directly)."""
    global _OWNER
    _OWNER = server_mod
    g = globals()
    for name in _CALL_RAILS:
        g[name] = _RailProxy(server_mod, name)
    _fresh_bind()
    _status_impl = _onboarding_status

    def _status():
        _fresh_bind()
        return _status_impl()

    return (visualize, _bake_loop, _start_baker, _status,
            _BAKE_LOCK, _BAKE_STATE, _BAKE_QUEUE, _BAKE_WAKE, _BAKE_START_LOCK)
