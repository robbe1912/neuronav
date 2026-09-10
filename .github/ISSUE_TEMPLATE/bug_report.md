---
name: Bug report
about: A tool answered wrong, crashed, or degraded unexpectedly
labels: bug
---

**Repro** — the tool call and what came back (smallest case that still breaks):

**Config shape** — `include_dirs`/`extensions`/`exclude_dirs` + backend
(Ollama model, or the OpenAI-compatible endpoint kind). Describe the SHAPE,
do not paste private repo paths, file names, or code.

**Environment** — OS, python version, embed provider (`ollama`/`openai`),
wired via `onboard.py` or manual config:

**Expected / actual:**
