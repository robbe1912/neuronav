# OpenHands repository guidance

You are working on **neuronav**, a local code-intelligence tool. Before any
change:

1. Read `AGENTS.md` at the repo root — it is the binding law book
   (determinism, config/store safety, suite gate, conventions).
2. Every change lands via pull request; conventional commits, lowercase scope.
3. Gate before claiming done: `.venv/Scripts/python.exe -X utf8 tests/test_<name>.py`
   (Windows venv; on Linux use `.venv/bin/python`). Run the suites that cover
   the files you touched.
4. Never write into any `.neuronav/` directory except a scratch one you created
   with an explicit `state_dir` in a temp config. Never set `NEURONAV_CONFIG`
   globally.
5. Prefer minimal diffs. No new dependencies without explicit issue mandate.
