# nav split (issue #344): focused smokes for the three leaves — config
# precedence + the rebind-propagation law (attribute reads, never frozen
# from-imports), the embed contract's dynamic config reads, the rescan
# walk, process-global singleton identity across config_scope, the CLI
# contract survival (#332), and the atomic-migration law itself (no
# production module may reference nav.<rebindable> anymore — a from-import
# freeze or a missed caller would read a stale world after use_config).
# Hermetic: scratch corpus, FAKE embeds, per-command NEURONAV_CONFIG.
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from harness import check, finish


def main() -> None:
    import navconfig
    import navindex
    import navstore

    tmp = tempfile.TemporaryDirectory(prefix="nn-navsplit-", ignore_cleanup_errors=True)
    with tmp:
        root = Path(tmp.name)
        proj = root / "proj"
        (proj / "pkg").mkdir(parents=True)
        for i in range(4):
            (proj / "pkg" / f"m{i}.py").write_text(
                f"def f{i}(x: int) -> int:\n    return x + {i}\n",
                encoding="utf-8")
        cfg = proj / ".neuronav" / "config.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({
            "root": proj.resolve().as_posix(),
            "collection": "navsplit-smoke",
            "include_dirs": ["pkg", "pkg"],  # dup: dedupe law
            "extensions": [".py"],
            "exclude_dirs": [".git", "__pycache__", ".neuronav"],
            "state_dir": str((root / "state").resolve()),
        }), encoding="utf-8")

        # 1. config leaf: precedence — env beats project-local (issue #27),
        #    and the rebind PROPAGATES to every leaf (the split's core law)
        saved_env_cfg = None
        import os
        saved_env_cfg = os.environ.pop("NEURONAV_CONFIG", None)
        try:
            navconfig.use_config(cfg)
            check("use_config rebinds navconfig globals",
                  navconfig.CONFIG_PATH == cfg
                  and navconfig.ROOT == proj.resolve()
                  and navconfig.COLLECTION == "navsplit-smoke",
                  f"root={navconfig.ROOT.as_posix()}")
            check("rebind propagates to the store leaf's world",
                  navstore.client.__doc__ is not None
                  and str(navconfig.DB_DIR) in str(navstore._db_lock().lock_file),
                  str(navstore._db_lock().lock_file))
            stats = navindex.rescan()
            check("rescan walk dedupes overlapping include_dirs (4 files)",
                  stats["added"] == 4, str(stats))

            # 2. config_scope: exact restore across every leaf (issue #131)
            other = root / "other"
            (other / ".neuronav").mkdir(parents=True)
            cfg2 = other / ".neuronav" / "config.json"
            cfg2.write_text(json.dumps({
                "root": other.resolve().as_posix(),
                "collection": "other-smoke",
                "extensions": [".py"],
                "exclude_dirs": [".git"],
                "state_dir": str((root / "state2").resolve()),
                "embed_model": "scope-probe-model",
            }), encoding="utf-8")
            # GK rider (#351 gate leg): snapshot EVERY rebindable, not a
            # hand-picked tuple — a restore that skips one field (his
            # sabotage: EMBED_DIM) must fail here, not slip through.
            pre = {f: getattr(navconfig, f) for f in navconfig._CONFIG_FIELDS}
            with navconfig.config_scope(cfg2):
                scoped_ok = (navconfig.COLLECTION == "other-smoke"
                             and navconfig.STATE_DIR == (root / "state2").resolve())
            check("config_scope rebinds and exact-restores across leaves",
                  scoped_ok
                  and {f: getattr(navconfig, f) for f in navconfig._CONFIG_FIELDS} == pre,
                  f"post={ {f: getattr(navconfig, f) for f in navconfig._CONFIG_FIELDS} }")

            # 3. singleton identity: one lock object per store across
            #    alternation — per-leaf re-instantiation would reopen the
            #    races the lock exists to prevent (the issue's hard gate)
            boot_key = navconfig.store_key()
            l1 = navstore._db_lock()
            with navconfig.config_scope(cfg2):
                pass
            l2 = navstore._db_lock()
            check("store lock singleton survives a scope round-trip",
                  l1 is l2 and str(boot_key[0]) in str(l1.lock_file),
                  f"{l1.lock_file} vs {l2.lock_file}")

            # 4. embed contract reads config dynamically (fake mode honors
            #    EMBED_DIM after a rebind — the frozen-import hazard class)
            os.environ["NEURONAV_EMBED_FAKE"] = "1"
            v = navstore.embed(["hello"])[0]
            check("embed dims follow the live config",
                  len(v) == navconfig.EMBED_DIM, f"{len(v)}")

            # 5. store round-trip: count + clusters memo identity
            check("count serves the active store", navstore.count() == 4,
                  str(navstore.count()))
            c1 = navstore.clusters()
            c2 = navstore.clusters()
            check("clusters memo returns ONE list per (store, args)",
                  c1 is c2, "")
        finally:
            if saved_env_cfg is not None:
                os.environ["NEURONAV_CONFIG"] = saved_env_cfg
            navconfig.use_config(Path(cfg))  # leave the scratch world bound

        # 6. CLI contract survival (#332): trailing argv rejected loudly,
        #    exit 2, correct global-prefix form named
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(ROOT / "nav.py"),
             "rescan", "--config", str(cfg)],
            capture_output=True, text=True, cwd=str(ROOT),
            env={**{k: v for k, v in __import__("os").environ.items()
                   if k != "NEURONAV_CONFIG"},
                 "NEURONAV_CONFIG": str(cfg), "NEURONAV_EMBED_FAKE": "1"},
            check=False,
        )
        check("CLI rejects trailing --config loudly (issue #332)",
              r.returncode == 2 and "nav.py --config <path> rescan" in r.stderr,
              r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "")

        # 7. atomic-migration law: no production module may hold a
        #    `nav.<name>` attribute reference — every rebindable global
        #    now lives on a leaf, and a missed caller would read the
        #    pre-use_config world forever (nav.py itself may reference
        #    nothing but its own CLI; string literals don't count)
        import ast

        core = ["server.py", "graph.py", "onboard.py", "recall.py", "viz.py",
                "clusters.py", "explore.py", "memories.py", "archrules.py",
                "predicates.py", "bake/embeddings.py", "navconfig.py",
                "navstore.py", "navindex.py"]
        offenders = []
        for rel in core:
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id == "nav"):
                    offenders.append(f"{rel}:{node.lineno}:nav.{node.attr}")
        check("no `nav.X` attribute refs remain in production modules",
              not offenders, ", ".join(offenders[:4]))

        # 8. the leaves are import-light where the law demands it:
        #    extractors must not import any nav leaf at module level
        #    (registry acyclicity — navconfig imports extractors)
        et = ast.parse((ROOT / "extractors" / "python.py").read_text(encoding="utf-8"))
        top_imports = [
            n for n in et.body
            if isinstance(n, (ast.Import, ast.ImportFrom))
            and any(
                (a.name.split(".")[0] if isinstance(n, ast.Import)
                 else (n.module or "").split(".")[0]) in
                ("nav", "navconfig", "navstore", "navindex")
                for a in (n.names if isinstance(n, ast.Import) else [None])
                if a is not None
            )
        ]
        check("extractors stay nav-leaf-import-light (registry acyclic)",
              not top_imports, f"{len(top_imports)} module-level import(s)")

    finish()


if __name__ == "__main__":
    main()
