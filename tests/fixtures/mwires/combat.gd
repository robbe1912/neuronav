class_name Combat
extends Node

#- const PlayerScene = player.gd
const PlayerScene = preload("res://player.gd")

#- @strike defines func
#- @strike calls @take_damage
func strike() -> void:
	var p: Player = PlayerScene.new()
	p.take_damage(5)
	p.health = p.health - 5

#- @tune_shield defines func
func tune_shield() -> void:
	PlayerScene.shield = 7
