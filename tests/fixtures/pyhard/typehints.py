"""Fixture: subscripted type hints resolve value-type references.

self.peers / extra are dict[str, Widget] — subscript access yields a
Widget receiver. An annotated local (local: Widget = ...) binds the
same way for later attribute calls.
"""

#- @sweep_all ret None
#- @merge_peers ret None


class Widget:
    def __init__(self):
        self.peers: dict[str, Widget] = {}

    def refresh(self) -> None:
        return None

    def retire(self) -> None:
        return None

    def sweep_all(self, extra: dict[str, Widget]) -> None:
        self.peers["first"].refresh()  # member hint -> value type
        extra["second"].retire()  # param hint -> value type
        local: Widget = self.peers["third"]
        local.refresh()  # annotated local receiver


def merge_peers(extra: dict[str, Widget]) -> None:
    extra.update({})  # param mutation via mutating method call


def sweep(w: Widget) -> None:
    w.sweep_all({})
    merge_peers({})


def unused_hint() -> int:
    return 7


if __name__ == "__main__":
    sweep(Widget())
