"""Fixture: decorator liveness for the python extractor.

@property getters/setters fire on attribute access — there is no call
site for ``total`` anywhere. @staticmethod/@classmethod methods are
invoked through normal call syntax. unused_helper is the dead control.
"""

from __future__ import annotations


class Counter:
    def __init__(self):
        self.n = 0

#- @total defines func
#- entry total
    @property
    def total(self) -> int:
        """Read via ``c.total`` — framework-dispatched, never called."""
        return self.n * 2

    @total.setter
    def total(self, value: int) -> None:
        """Written via ``c.total = x`` — same attribute-dispatch story."""
        self.n = value // 2

    @staticmethod
    def describe(label: str) -> str:
        return f"counter {label}"

    @classmethod
    def with_start(cls, start: int) -> "Counter":
        obj = cls()
        obj.n = start
        return obj


#- @unused_helper defines func
#- @unused_helper dead
def unused_helper() -> int:
    return -1


#- @use_all defines func
#- @use_all calls @describe
#- @use_all calls @with_start
def use_all(c: Counter) -> str:
    c.total = 10  # attribute write fires the setter
    fresh = Counter.with_start(4)  # classmethod via ordinary call syntax
    return Counter.describe(f"{c.total}{fresh.n}")  # read fires the getter


if __name__ == "__main__":
    _c = Counter()
    _r = use_all(_c)
    print(_r, Counter.with_start(1), _c.total)
