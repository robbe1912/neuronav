class_name Combat
extends Node

const PlayerScene = preload("res://player.gd")

func strike() -> void:
	var p: Player = PlayerScene.new()
	p.take_damage(5)
	p.health = p.health - 5

func tune_shield() -> void:
	PlayerScene.shield = 7
