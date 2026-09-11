# Fixture: column-0 lines inside triple-quoted strings must not truncate
# the set(v): accessor body scan (issue #50, gd side — the accessor scan
# is indent-delimited like the func scan, and must track string state).
extends Node


#- @_set_payload defines func
#- @_set_payload calls @edge_after_setter
#- entry _set_payload
@export var payload: String = "":
	set(v):
		payload = """
column-0 text inside the string — NOT a dedent
"""
		edge_after_setter()


#- @edge_after_setter defines func
func edge_after_setter() -> int:
	return 1


#- @unused_gd_helper defines func
#- @unused_gd_helper dead
func unused_gd_helper() -> int:
	return 2
