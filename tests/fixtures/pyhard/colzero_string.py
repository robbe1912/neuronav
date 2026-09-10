"""Fixture: column-0 lines inside triple-quoted strings must not truncate
the per-fn body view (issue #50).

payload() carries a multiline string whose content sits at column 0 —
string content, not a dedent. The indent scan read it as one and lost
everything after it; the AST end_lineno span must keep the call edge
after the string in the per-fn body view, keeping both funcs alive.
unused_colzero_helper stays dead: nothing references it.
"""


def payload() -> str:
    doc = """
column-0 text inside the string — NOT a dedent
    indented text inside the string
"""
    edge_after_string()
    return doc


def edge_after_string() -> int:
    return 1


payload()


def unused_colzero_helper() -> int:
    return 2
