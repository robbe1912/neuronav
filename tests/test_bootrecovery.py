# boot recovery + bounded rescan (issue #292) — run in its own process:
#   .venv/Scripts/python.exe -X utf8 tests/test_bootrecovery.py [server_dir]
#
# Hermetic: a scratch md-only root bound as nav's PURE-DEFAULTS boot (no
# NEURONAV_CONFIG anywhere — the suite scrubs it), FAKE embeds, stores
# under the scratch root only. server_dir defaults to the checkout
# holding this tests/ tree; passing a different checkout runs the same
# legs against that code (how the pre-fix FAIL evidence was recorded —
# test_bootgate's argv pattern).
#
# Pins the issue #292 contract:
#   (a) one failed recovery attempt must not brick the session: the
#       re-bind rolls back (nav globals + env + boot identity), the
#       next call retries and recovers
#   (b) nav's lock-timeout SystemExit lands in the degraded prelude
#       instead of escaping _boot_recovery (which kills the caller's
#       thread mid-recovery) — and still rolls back + retries
#   (c) the explicit rescan tool rides the bounded store-lock wait: a
#       held lock aborts loudly within the bound naming lock + likely
#       holder, never an infinite queue (the #203/#206 law extended to
#       the last unbounded sites)
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

from filelock import FileLock  # the fake second process's lock holder

SERVER_DIR = (
    Path(sys.argv[1]).resolve() if len(sys.argv) > 1
    else Path(__file__).resolve().parents[1]
)
sys.path.insert(0, str(SERVER_DIR))  # the checkout under test wins over any editables



import harness

check = harness.styled("colon")  # byte pin: "PASS: <name>" lines

def main() -> None:
    # best-effort cleanup: chroma keeps sqlite handles past teardown on
    # Windows — a cleanup failure must never mask the verdict
    with tempfile.TemporaryDirectory(
        prefix="nn-bootrecovery-", ignore_cleanup_errors=True
    ) as td:
        root = Path(td) / "mdrepo"
        root.mkdir()
        (root / "note_a.md").write_text(
            "alpha dossier one.\nsome raw text to embed.\n", encoding="utf-8"
        )
        (root / "note_b.md").write_text(
            "bravo dossier two.\ndifferent raw text.\n", encoding="utf-8"
        )
        cfg_path = root / ".neuronav" / "config.json"

        # nav binds PURE DEFAULTS at import: no env config, and cwd is
        # the scratch root BEFORE `import nav` (nav resolves ROOT from
        # cwd; discovery would also find .neuronav/config.json, which
        # does not exist yet — the config appears mid-session below)
        os.environ.pop("NEURONAV_CONFIG", None)
        os.environ.setdefault("NEURONAV_EMBED_FAKE", "1")
        os.chdir(root)

        import nav  # noqa: E402  (binds the scratch root, after chdir by design)
        import graph  # noqa: E402
        import server  # noqa: E402  (import runs no boot work — only main() does)

        check(
            "setup: pure-defaults boot (no config bound)",
            nav.CONFIG_PATH is None and nav.ROOT == root.resolve(),
            f"CONFIG_PATH={nav.CONFIG_PATH} ROOT={nav.ROOT}",
        )

        degraded = "neuronav: EMPTY INDEX — degraded stand-in"
        server._BOOT_DEGRADED = degraded

        def write_config() -> None:
            # the guidance's paste-ready block (state_dir "default" is
            # the #91 opt-in; .neuronav never walks)
            cfg_path.parent.mkdir(exist_ok=True)
            cfg_path.write_text(
                json.dumps(
                    {
                        "root": "..",
                        "collection": "bootrecovery",
                        "state_dir": "default",
                        "include_dirs": ["."],
                        "extensions": [".md"],
                        "exclude_dirs": [],
                    }
                ),
                encoding="utf-8",
            )

        def reset_degraded() -> None:
            # back to the pure-defaults degraded boot between legs: the
            # env use_config exported plus the globals it rebound
            os.environ.pop("NEURONAV_CONFIG", None)
            nav._apply_config(None)
            server._BOOT_DEGRADED = degraded
            server._BOOT_STORE = (str(nav.STATE_DIR), nav.COLLECTION)
            graph._graph = None

        # ---- (a) failed recovery must roll back and stay retryable ----
        write_config()
        real_rescan = server._bounded_rescan

        def _boom(*_a, **_k):
            raise RuntimeError("embed backend unreachable (stubbed)")

        server._bounded_rescan = _boom
        prelude = None
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                with server._route("") as prelude:
                    pass  # the degraded branch yields; the body never runs
        finally:
            server._bounded_rescan = real_rescan

        check(
            "recovery-fail: tools answer the degraded prelude",
            prelude is not None and "recovery FAILED" in prelude,
            "" if prelude else "no prelude",
        )
        check(
            "recovery-fail: prelude says the next call retries (not restart)",
            prelude is not None and "next call retries" in prelude,
            (prelude or "")[-120:],
        )
        check(
            "recovery-fail: config re-bind rolled back (CONFIG_PATH None)",
            nav.CONFIG_PATH is None,
            f"CONFIG_PATH={nav.CONFIG_PATH}",
        )
        check(
            "recovery-fail: NEURONAV_CONFIG env rolled back",
            "NEURONAV_CONFIG" not in os.environ,
            os.environ.get("NEURONAV_CONFIG", ""),
        )
        with server._route("") as prelude2:
            pass
        check(
            "recovery-retry: next call recovers instead of bricked guidance",
            prelude2 is not None and "rebound the boot" in prelude2,
            (prelude2 or "")[:120],
        )
        check(
            "recovery-retry: boot now bound to the appeared config",
            nav.CONFIG_PATH == cfg_path.resolve(),
            f"CONFIG_PATH={nav.CONFIG_PATH}",
        )
        served = server.semantic_search("dossier", 2)
        check(
            "recovery-retry: tools serve the recovered index",
            "note_a.md" in served,
            served[:140],
        )

        # ---- (b) SystemExit from the lock timeout lands degraded -----
        reset_degraded()
        lock_abort = (
            "neuronav: gave up after 0.5s waiting for the store write "
            f"{root.resolve() / '.neuronav' / 'chroma' / '.write.lock'} — "
            "another neuronav process (server, CLI rescan or viz bake) "
            "holds it; end that process and retry."
        )

        def _exit(*_a, **_k):
            raise SystemExit(lock_abort)

        server._bounded_rescan = _exit
        rec = None
        escaped: BaseException | None = None
        try:
            rec = server._boot_recovery()
        except SystemExit as e:  # pre-fix: the abort kills the caller's thread
            escaped = e
        finally:
            server._bounded_rescan = real_rescan
        check(
            "lock-timeout: SystemExit caught, not escaping to the thread",
            escaped is None and rec is not None and "recovery FAILED" in rec,
            f"escaped={escaped!r}",
        )
        check(
            "lock-timeout: abort text rides the degraded prelude (loud)",
            rec is not None and "gave up after" in rec and "holds it" in rec,
            (rec or "")[-160:],
        )
        check(
            "lock-timeout: re-bind rolled back too",
            nav.CONFIG_PATH is None,
            f"CONFIG_PATH={nav.CONFIG_PATH}",
        )
        rec2 = server._boot_recovery()
        check(
            "lock-timeout: retry after the holder exits recovers",
            rec2 is not None and "rebound the boot" in rec2,
            (rec2 or "")[:120],
        )

        # ---- (c) explicit rescan tool: bounded wait under a held lock --
        holder = FileLock(str(nav.DB_DIR / ".write.lock"))
        holder.acquire()
        real_wait = server.LOCK_WAIT_S
        server.LOCK_WAIT_S = 0.5
        outcome: dict = {}

        def _rescan_call() -> None:
            try:
                outcome["out"] = server.rescan("")
            except BaseException as e:  # SystemExit is the expected loud abort
                outcome["exc"] = e

        try:
            t0 = time.monotonic()
            worker = threading.Thread(target=_rescan_call, daemon=True)
            worker.start()
            worker.join(6.0)
            elapsed = time.monotonic() - t0
            done = not worker.is_alive()
            check(
                "bounded rescan: aborts within the bound, never queues forever",
                done,
                f"aborted in {elapsed:.1f}s" if done
                else f"still waiting after 6.0s ({elapsed:.1f}s) — unbounded wait",
            )
            exc = outcome.get("exc")
            check(
                "bounded rescan: loud SystemExit names lock + likely holder",
                isinstance(exc, SystemExit)
                and "gave up after 0.5s" in str(exc)
                and ".write.lock" in str(exc)
                and "another neuronav process" in str(exc),
                repr(str(exc)[:200]) if exc is not None else f"returned {outcome.get('out')!r}",
            )
        finally:
            server.LOCK_WAIT_S = real_wait
            holder.release()
        warm = server.rescan("")
        check(
            "bounded rescan: lock released, the tool serves again",
            isinstance(warm, str) and warm.startswith("rescan: files"),
            str(warm)[:140],
        )

    # byte pin: pass/total ratio summary, now off the shared sink
    print(f"\n{harness.EXECUTED - len(harness.FAILURES)}/{harness.EXECUTED} checks passed")
    if harness.FAILURES:
        sys.exit(1)


if __name__ == "__main__":
    main()
