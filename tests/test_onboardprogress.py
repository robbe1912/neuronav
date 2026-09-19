# onboarding progress (issue #315): the first index build and the viz bake
# must never read as a dead server. Every MCP call during minutes of silent
# embed/graph/bake work used to surface as the client's generic -32001
# timeout, indistinguishable from a crashed process. The contract now:
#   * neuronav://onboarding/status — a pollable resource that always answers
#   * notifications/progress — phase/count/total streamed to tokened callers
#   * visualize acks "bake accepted" immediately; the bake lands in the
#     background and its result surfaces via stderr + the resource
#   * repo_map/semantic_search serve partially-built reads tagged
#     stale: true — but only past a grace window, so short builds still
#     answer fresh
# Over stdio (legs 1-3, a spawned server-under-test): resource advertised +
# readable, a progressToken'd rescan receives a progress notification, and
# the visualize ack is immediate with the bake landing asynchronously.
# In-process (legs 4-5): the stale grace window and the unwrapped-SystemExit
# guarantee of the async shell.
#   .venv/Scripts/python.exe -X utf8 tests/test_onboardprogress.py [server_dir]
# (hermetic temp fixture + config, NEURONAV_EMBED_FAKE=1; server-under-test
# selectable via argv for pre-fix FAIL evidence, test_bootgate precedent)
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SERVER_DIR = (
    Path(sys.argv[1]).resolve() if len(sys.argv) > 1
    else Path(__file__).resolve().parents[1]
)
sys.path.insert(0, str(SERVER_DIR))  # the checkout under test wins over editables

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}: {name}" + (f" — {detail}" if detail else ""))


def main() -> None:
    tmp = tempfile.TemporaryDirectory(prefix="nn-onboardprog-", ignore_cleanup_errors=True)
    with tmp:
        root = Path(tmp.name)
        proj = root / "proj"
        (proj / "pkg").mkdir(parents=True)
        # enough import-chained modules that a fresh rescan spans at least
        # one notification poll tick (server.PROGRESS_POLL_S = 2s)
        for i in range(220):
            mod = proj / "pkg" / f"mod{i:03d}.py"
            if i == 0:
                mod.write_text(
                    "def core_anchor_run(scale: int) -> int:\n"
                    "    return scale * 2\n", encoding="utf-8")
            else:
                mod.write_text(
                    f"from pkg.mod{i - 1:03d} import core_anchor_run\n\n\n"
                    f"def chain_{i:03d}_run(x: int) -> int:\n"
                    f"    return core_anchor_run(x) + {i}\n", encoding="utf-8")

        # a second, larger fresh project: the tokened first-contact rescan
        # against it stays IN FLIGHT through the whole embed phase — the
        # only window in which notifications/progress can stream (the call
        # owns its poller; visualize's bake outlives its ack by design)
        big = root / "bigproj"
        (big / "pkg").mkdir(parents=True)
        for i in range(600):
            mod = big / "pkg" / f"big{i:03d}.py"
            if i == 0:
                mod.write_text(
                    "def big_anchor_run(scale: int) -> int:\n"
                    "    return scale * 3\n", encoding="utf-8")
            else:
                mod.write_text(
                    f"from pkg.big{i - 1:03d} import big_anchor_run\n\n\n"
                    f"def big_chain_{i:03d}_run(x: int) -> int:\n"
                    f"    return big_anchor_run(x) + {i}\n", encoding="utf-8")
        cfg_path = proj / ".neuronav" / "config.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps({
            "root": proj.resolve().as_posix(),
            "collection": "onboardprog",
            "include_dirs": ["pkg"],
            "extensions": [".py"],
            "exclude_dirs": [".git", "__pycache__", ".venv", ".neuronav"],
            "state_dir": "default",
        }), encoding="utf-8")

        env = {k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"}
        env["NEURONAV_CONFIG"] = str(cfg_path)
        env.setdefault("NEURONAV_EMBED_FAKE", "1")

        proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(SERVER_DIR / "server.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", cwd=str(SERVER_DIR), env=env,
        )
        out_q: "queue.SimpleQueue[str]" = queue.SimpleQueue()  # type: ignore[name-defined]
        err_lines: list[str] = []

        def _tap_err() -> None:
            assert proc.stderr is not None
            for ln in proc.stderr:
                err_lines.append(ln.rstrip("\n"))

        threading.Thread(target=_tap_err, daemon=True).start()

        def send(payload: dict) -> None:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()

        def recv(want_id: int, deadline_s: float = 90.0) -> dict | None:
            """Scan stdout for the JSON-RPC answer with our id; park
            non-matching lines (notifications) on the tap queue."""
            t0 = time.monotonic()
            while time.monotonic() - t0 < deadline_s:
                while not out_q.empty():
                    ln = out_q.get()
                    try:
                        msg = json.loads(ln)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("id") == want_id:
                        return msg
                    if msg.get("method") is not None:
                        notes.append(msg)
                if proc.poll() is not None:
                    return None
                time.sleep(0.05)
            return None

        def _tap_out() -> None:
            assert proc.stdout is not None
            for ln in proc.stdout:
                out_q.put(ln.rstrip("\n"))

        threading.Thread(target=_tap_out, daemon=True).start()
        notes: list[dict] = []  # notifications/progress land here

        try:
            send({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "onboardprog-test", "version": "0"},
            }})
            init = recv(0)
            check("onboardprog: initialize handshake", init is not None)
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})

            # leg 1 — the pollable status resource (advertised + readable)
            send({"jsonrpc": "2.0", "id": 1, "method": "resources/list"})
            rlist = recv(1)
            uris = []
            if rlist is not None:
                for r in rlist.get("result", {}).get("resources", []):
                    uris.append(r.get("uri", ""))
            check(
                "onboardprog: onboarding/status resource advertised",
                "neuronav://onboarding/status" in uris,
                f"resources: {uris}",
            )
            send({"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {
                "uri": "neuronav://onboarding/status"}})
            rread = recv(2)
            status_text = ""
            if rread is not None:
                for c in rread.get("result", {}).get("contents", []):
                    status_text += c.get("text", "")
            check(
                "onboardprog: status resource answers with boot state",
                "boot:" in status_text,
                status_text[:120],
            )

            # leg 2 — a progressToken'd FIRST-CONTACT rescan stays in flight
            # through the big project's embed phase and streams
            # notifications/progress while it builds
            t_first = time.monotonic()
            send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "rescan", "arguments": {"dir": str(big)},
                "_meta": {"progressToken": 9901}}})
            ans = recv(3, deadline_s=180.0)
            first_elapsed = time.monotonic() - t_first
            rescan_text = ""
            if ans is not None:
                for c in ans.get("result", {}).get("content", []):
                    rescan_text += c.get("text", "")
            check(
                "onboardprog: progressToken'd first-contact rescan streams notifications/progress",
                any(n.get("method") == "notifications/progress"
                    and n.get("params", {}).get("progressToken") == 9901
                    for n in notes),
                f"{len(notes)} notification(s) tapped; answer: {rescan_text[:70]}",
            )
            check(
                "onboardprog: the first-contact call still answers with the onboarded summary",
                rescan_text.startswith("onboarded") and first_elapsed > 2.0,
                f"{first_elapsed:.1f}s — {rescan_text[:80]}",
            )

            # leg 3 — visualize acks immediately; the bake runs in background
            # (minutes on a real store, ~4s on this fixture) — the honest
            # bake channels are stderr + the status resource (the call's
            # poller ends at the ack; the bake outlives it by design)
            t_ack = time.monotonic()
            send({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                "name": "visualize", "arguments": {}}})
            ack = recv(4, deadline_s=60.0)
            ack_elapsed = time.monotonic() - t_ack

            ack_text = ""
            if ack is not None:
                for c in ack.get("result", {}).get("content", []):
                    ack_text += c.get("text", "")
            check(
                "onboardprog: visualize acks 'bake accepted' immediately",
                ack_text.startswith("bake accepted") and ack_elapsed < 30.0,
                f"{ack_elapsed:.1f}s — {ack_text[:100]}",
            )
            baked = ""
            t_bake = time.monotonic()
            while time.monotonic() - t_bake < 90.0:
                send({"jsonrpc": "2.0", "id": 5, "method": "resources/read", "params": {
                    "uri": "neuronav://onboarding/status"}})
                rr = recv(5, deadline_s=30.0)
                if rr is not None:
                    baked = "".join(c.get("text", "")
                                    for c in rr.get("result", {}).get("contents", []))
                    if "bake: done" in baked or "bake: FAILED" in baked:
                        break
                time.sleep(0.5)
            check(
                "onboardprog: bake lands via the background baker",
                "bake: done" in baked and (proj / ".neuronav" / "graph.html").is_file(),
                baked[:140],
            )
        finally:
            proc.kill()
            proc.wait(timeout=10)
            if any(not ok for _, ok, _ in RESULTS):
                print("--- server stderr tail ---")
                for ln in err_lines[-15:]:
                    print(ln)

        # legs 4-5 — in-process: stale grace window + unwrapped SystemExit
        os.environ["NEURONAV_CONFIG"] = str(cfg_path)
        os.environ.setdefault("NEURONAV_EMBED_FAKE", "1")
        import anyio  # noqa: E402  (server's async shells)
        import asyncio  # noqa: E402
        import navindex
        import server  # noqa: E402

        navindex.import_base()
        stats = navindex.rescan()
        server._sync_chain(stats)
        navindex.stat_mark_synced()

        saved_thread, was_ready = server._BOOT_THREAD, server._BOOT_READY.is_set()
        try:
            # fake the boot-building window without a real boot thread
            server._BOOT_READY.clear()
            server._BOOT_THREAD = threading.enumerate()[0]  # truthy sentinel
            server._BOOT_FATAL = None

            server._BOOT_T0 = time.monotonic()  # inside the grace window
            # the decision is asserted directly: a fresh-path repo_map call
            # would then block on the (faked, never-setting) boot gate —
            # that gate IS the pre-#315 contract for short builds
            check(
                "onboardprog: inside the grace window the read stays fresh (no stale banner)",
                server._stale_prelude() is None,
                "stale banner served inside the grace window",
            )

            server._BOOT_T0 = time.monotonic() - server.PROGRESS_STALE_GRACE_S - 1.0
            stale_map = asyncio.run(server.repo_map())
            check(
                "onboardprog: past the grace window repo_map serves stale: true",
                stale_map.startswith("stale: true") and "you are here:" in stale_map,
                stale_map[:120],
            )
            stale_sem = asyncio.run(server.semantic_search("core anchor run", 4))
            check(
                "onboardprog: past the grace window semantic_search serves stale",
                stale_sem.startswith("stale: true"),
                stale_sem[:120],
            )
        finally:
            server._BOOT_THREAD, server._BOOT_FATAL = saved_thread, None
            if was_ready:
                server._BOOT_READY.set()

        def _boom():
            raise SystemExit(
                "neuronav: gave up after 0.5s waiting for the store write lock "
                "— another neuronav process holds it (probe)"
            )

        try:
            anyio.run(server._serve, _boom, None)
            escaped = None
        except BaseException as e:  # noqa: BLE001 — the contract IS the escape
            escaped = e
        check(
            "onboardprog: a body SystemExit surfaces unwrapped from the shell",
            type(escaped) is SystemExit and "gave up after" in str(escaped),
            f"{type(escaped).__name__}: {str(escaped)[:100]}",
        )

    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
