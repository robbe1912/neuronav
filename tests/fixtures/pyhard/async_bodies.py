"""Fixture: async-for/async-with bodies are scanned; with-targets typed.

Calls inside async-for and async-with blocks must produce edges, and
``async with Session() as s`` binds ``s: Session`` so s.migrate()
resolves — same for the plain ``with`` form.
"""


class Session:
    def migrate(self) -> None:
        return None

    def verify(self) -> bool:
        return True


async def run_upgrade(db) -> None:
    async with Session() as s:
        s.migrate()
    async for row in stream_rows(db):
        handle_row(row)


def stream_rows(db):
    return []


def handle_row(row) -> None:
    return None


def run_check() -> bool:
    with Session() as s:
        return s.verify()


def unused_async() -> int:
    return 9


if __name__ == "__main__":
    _u = run_upgrade(None)
    _c = run_check()
