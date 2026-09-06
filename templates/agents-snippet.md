# neuronav agent guidance — paste into your project's AGENTS.md

Adjust the include-dirs phrase and tool list to taste; keep the priority rule
verbatim — it is the part that changes agent behavior.

---

**neuronav (code intelligence — USE FIRST for code questions)**

- Vector recall + call graph over THIS repo: `semantic_search`, `find_functions`
  (line-numbered source slices), `symbol_graph` (callers/callees), `explore`
  (one-call orientation: code + flow + refs), `clusters`, `dead_code`
  (candidates — read before deleting), `duplicates`, `rescan` (run after big
  refactors).
- **Priority rule**: "how does X work / where is X / what calls Y" → neuronav
  FIRST (one call replaces grep+read loops). Grep/Glob stay SECONDARY:
  exact-string search, known file paths, or when neuronav is not wired.
- Setup if unwired: see this repo's neuronav wiring docs (`.neuronav/` or
  equivalent) — needs a neuronav clone + Ollama with `qwen3-embedding:0.6b`.
