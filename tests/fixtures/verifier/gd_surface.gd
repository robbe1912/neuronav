# Synthetic declared-surface fixture for the verifier (issue #66): gd
# extractor facts with no committed coverage (see test_verifier.py).
#- tool
@tool
#- class GdSurface
class_name GdSurface
#- extends Node
extends Node
#- signal spun_up
signal spun_up
#- member cache : Dictionary
var cache: Dictionary = {}
#- const HELPER_SCRIPT = gd_surface_helper.gd
const HELPER_SCRIPT = preload("res://gd_surface_helper.gd")
#- literal on_spin_up
@export var hook: StringName = &"on_spin_up"
#- member curve : Curve
#- init-call build_curve
var curve: Curve = build_curve()
#- entry net_sync
@rpc
func net_sync(delta: int) -> void:
	pass
#- @_set_total defines func
#- @_set_total calls @apply_clamp
#- entry _set_total
@export var total: int = 0:
	set(v):
		total = v
		apply_clamp(v)
#- @apply_clamp defines func
#- @apply_clamp ret int
#- @apply_clamp writes total
func apply_clamp(v: int) -> int:
	self.total = clampi(v, 0, 100)
	return self.total
#- @spin calls @spin_inner
func spin(times: int) -> void:
	spin_inner(times)
#- @spin_inner defines func
func spin_inner(times: int) -> void:
	cache[times] = total
