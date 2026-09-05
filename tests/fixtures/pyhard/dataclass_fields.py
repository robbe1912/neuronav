"""Fixture: @dataclass fields as members — typed chains into vec2.Vec2.

label: str is a lowercase-builtin field; delta: Vec2 is a repo-class
field whose member record feeds the self.delta.norm() cross-file chain.
"""

from dataclasses import dataclass

from vec2 import Vec2


@dataclass
class Move:
    delta: Vec2 = None
    label: str = ""

    def length(self) -> float:
        return self.delta.norm()


def use_move(m: Move) -> str:
    return f"{m.length():.1f} {m.label}"


def unused_move() -> int:
    return 2


if __name__ == "__main__":
    print(use_move(Move(delta=Vec2(3, 4), label="diag")))
