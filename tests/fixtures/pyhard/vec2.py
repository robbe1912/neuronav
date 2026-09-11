"""Fixture: @dataclass with lowercase builtin field types.

Paired with dataclass_fields.py: norm() has no local caller — it stays
alive only through the cross-file typed chain from Move.length.
"""

from dataclasses import dataclass


@dataclass
class Vec2:
#- member x : float
    x: float = 0.0
#- member y : float
    y: float = 0.0

#- @norm defines func
    def norm(self) -> float:
        return (self.x * self.x + self.y * self.y) ** 0.5


#- @unused_vec defines func
#- @unused_vec dead
def unused_vec() -> int:
    return 1
