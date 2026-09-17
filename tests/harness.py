# tests/harness.py — the shared script-suite harness (issue #301 A).
#
# ONE home for the check()/finish() contract every tests/test_*.py suite
# rides. Before #301 each suite carried its own copy (~40 at main) and
# they had drifted into six output shapes; a check-contract change (skip
# semantics, #97 gating) had to be ported N times and historically missed
# some. A suite now does:
#
#     from harness import check, finish    # canonical style
#     ...
#     finish()                             # "\n<N> failure(s)" + exit 1 on any
#
# Import works because every suite runs as `python tests/test_<name>.py`
# (tests/ is sys.path[0]); the browser suites keep _page_harness.CheckLog
# (its finish() carries the #123 executed-check floor — a different
# contract, not drift).
#
# BYTE-FREEZE LAW: PASS/FAIL lines and summary tails are output pins —
# battery logs are diffed run over run. The styles below reproduce the
# pre-#301 bytes exactly; they are pins, not choices. A suite whose
# pre-#301 shape differs keeps its bytes via styled("<name>") for the
# check line and/or its own tail lines against the shared FAILURES sink
# (each survivor tail in a suite carries a named reason). New suites use
# the canonical style only.
import sys

# One sink per process — every suite runs as its own process (AGENTS.md).
FAILURES: list[str] = []
EXECUTED = 0  # checks run; ratio-style summaries (bootgate) need the count


def _emit(style: str, name, cond, detail) -> None:
    tag = "PASS" if cond else "FAIL"
    if style == "dash":  # canonical: 'PASS <name>' + ' — <detail>' when set
        print(f"{tag} {name}" + (f" — {detail}" if detail else ""))
    elif style == "wide":  # two-space tag + fail-only two-space detail
        print(f"{tag}  {name}" + (f"  {detail}" if detail and not cond else ""))
    elif style == "comma":  # print-sep args; empty detail still rides the sep
        print(tag, name, detail)
    elif style == "colon":  # 'PASS: <name>' + ' — <detail>' when set
        print(f"{tag}: {name}" + (f" — {detail}" if detail else ""))
    elif style == "faildash":  # canonical tag + fail-only ' — <detail>'
        print(f"{tag} {name}" + (f" — {detail}" if detail and not cond else ""))
    elif style == "bracket":  # '[PASS] <name>' + fail-only ' — <detail>'
        print(f"[{tag}] {name}" + (f" — {detail}" if detail and not cond else ""))
    else:
        raise ValueError(f"harness: unknown check style {style!r}")


def check(name, cond, detail=""):
    """Canonical check line: 'PASS <name>' / 'FAIL <name>' plus
    ' — <detail>' whenever detail is set. Failures land in FAILURES."""
    global EXECUTED
    EXECUTED += 1
    _emit("dash", name, cond, detail)
    if not cond:
        FAILURES.append(name)


def finish():
    """Canonical failure summary + exit contract: prints
    '\\n<N> failure(s)' and exits 1 iff any check failed. Suites with a
    different pre-#301 summary keep their own tail lines against
    FAILURES (byte pins — see module header)."""
    print(f"\n{len(FAILURES)} failure(s)")
    sys.exit(1 if FAILURES else 0)


def styled(style: str):
    """check() emitting a pre-#301 PASS/FAIL line style — a byte pin, not
    a choice. Same sink and contract as the canonical check:

        check = styled("comma")

    Styles are frozen: adding one requires naming the suite bytes it
    preserves."""
    def check(name, cond, detail=""):
        global EXECUTED
        EXECUTED += 1
        _emit(style, name, cond, detail)
        if not cond:
            FAILURES.append(name)
    return check
