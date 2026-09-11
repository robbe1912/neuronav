"""Fixture: module-level UPPER_CASE assigns harvest into fs.consts as
symbols (exact-identifier recall), never into fs.funcs. String-literal
values keep the consts-as-path contract (res:// stripped, mirroring .gd
const preloads); annotated and non-literal consts store ''.
"""

import re

#- const STRINGNAME_LIT_RE
STRINGNAME_LIT_RE = re.compile(r"^&\"([^\"]+)\"$")

#- const CONFIG_PATH = config/default.json
CONFIG_PATH = "res://config/default.json"

#- const BUDGET_TOKENS
BUDGET_TOKENS: int = 512

#- !const lower_counter
lower_counter = 0  # lowercase module assign: NOT a const


#- @use_consts defines func
def use_consts(s: str) -> bool:
    return bool(STRINGNAME_LIT_RE.match(s)) and CONFIG_PATH != s


#- @unused_const_helper defines func
#- @unused_const_helper dead
def unused_const_helper(x: int) -> int:
    return x + BUDGET_TOKENS


use_consts("&\"npc\"")
