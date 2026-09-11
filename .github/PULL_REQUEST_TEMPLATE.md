<!-- Title: conventional commit, lowercase scope — feat(scope): / fix: / docs: -->

## What and why

<!-- What changes, why now. Link the issue: "Closes #N". -->

## Pair reviewer

<!-- Convention: name the reviewer agent in the draft PR body at open time. -->

- Reviewer: ______ — grounded each increment (claims vs real runs, real
  numbers, real files) before push.

## Gate

CI is the minimum bar (hermetic six: `test_strata`, `test_crosslang`,
`test_pyhard`, `test_cpphard`, `test_autorescan`, `test_project_mode`).

- [ ] CI green
- [ ] Extra local suites this change touches: <!-- e.g. test_viz (needs a fresh bake), test_target_regression, test_server_stdio, test_mwires, test_embedprov -->

## Evidence

<!-- Output tails, bake diffs, screenshots parked under .tmp/ — never commit
     target-repo data (names, paths, measurements). -->

## Determinism (layout/export changes only)

- [ ] Same DATA -> byte-identical bake; no unordered iteration introduced.
