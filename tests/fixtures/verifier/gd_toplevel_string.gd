# File-top triple-quoted strings are data, not code (issue #106): the
# license-header / help-text / template-constant shapes must yield zero
# declarations while real ones after them still parse.
#- !@phantom_in_string defines func
#- !signal ghost_in_string
"""
usage: attach and forget
func phantom_in_string() -> int:
	return 1
signal ghost_in_string
"""
extends Node
#- extends Node
# multi-line template constant: content indented, opener mid-line
const TPL = """
	template body
	func another_phantom():
"""
#- !@another_phantom defines func
const HELP = '''
	func not_a_func(
'''
#- !@not_a_func defines func
signal real_ping
#- signal real_ping
func real_one():
	helper()
#- @real_one defines func
#- @real_one calls @helper
#- @helper defines func
func helper() -> void:
	pass
