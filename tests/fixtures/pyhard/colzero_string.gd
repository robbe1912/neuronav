# Fixture: column-0 lines inside triple-quoted strings must not truncate
# the set(v): accessor body scan (issue #50, gd side — the accessor scan
# is indent-delimited like the func scan, and must track string state).
extends Node


@export var payload: String = "":
	set(v):
		payload = """
column-0 text inside the string — NOT a dedent
"""
		edge_after_setter()


func edge_after_setter() -> int:
	return 1


func unused_gd_helper() -> int:
	return 2
