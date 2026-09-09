class_name Player
extends Node

# capitalized member types — the graph only harvests class-shaped member
# decls (extractors MEMBER_TYPED_RE); Node keeps the fixture valid GDScript
var health: Node = null
var shield: Node = null

func take_damage(amount: int) -> void:
	health -= amount

func heal(amount: int) -> void:
	health += amount
