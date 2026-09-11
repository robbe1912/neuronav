#- class Player
#- extends Node
class_name Player
extends Node

# capitalized member types — the graph only harvests class-shaped member
# decls (extractors MEMBER_TYPED_RE); Node keeps the fixture valid GDScript
#- member health : Node
var health: Node = null
#- member shield : Node
var shield: Node = null

#- @take_damage defines func
func take_damage(amount: int) -> void:
	health -= amount

#- @heal defines func
func heal(amount: int) -> void:
	health += amount
