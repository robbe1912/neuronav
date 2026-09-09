"""Fixture: module-level UPPER_CASE assigns harvest into fs.consts as
symbols (exact-identifier recall), never into fs.funcs. String-literal
values keep the consts-as-path contract (res:// stripped, mirroring .gd
const preloads); annotated and non-literal consts store ''.
"""

import re

STRINGNAME_LIT_RE = re.compile(r"^&\"([^\"]+)\"$")

CONFIG_PATH = "res://config/default.json"

BUDGET_TOKENS: int = 512

lower_counter = 0  # lowercase module assign: NOT a const


def use_consts(s: str) -> bool:
    return bool(STRINGNAME_LIT_RE.match(s)) and CONFIG_PATH != s


def unused_const_helper(x: int) -> int:
    return x + BUDGET_TOKENS


use_consts("&\"npc\"")
