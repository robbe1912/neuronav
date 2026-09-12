"""consumer_imports: every import/liveness attribution shape of
#107/#153/#142 — parenthesized and commented from-imports, re-export
resolution to the defining module, function-local imports, module-scope
calls in indented blocks, value-ref assigns, module-level instance
receivers, and injected stand-in dispatch.
"""
from reexport_pkg import (  # multi-line parenthesized from-import
    alpha,
    beta as beta_alias,
)
from reexport_pkg import gamma  # trailing comment must not eat the name
from vec2 import norm as vec_norm


def _bump():
    return 1


def use_package_surface():
    alpha(1)
    beta_alias(2)
    gamma(3)
    return vec_norm(3.0, 4.0)


def lazy_import_caller():
    from reexport_pkg.provider import delta

    return delta(4)


class Ledger:
    def tally(self):
        return 1


class Probe:
    # stand-in injected into an opaque consumer: dispatch surface
    def probe_method(self):
        return 3


class UnusedLedger:
    # control: never referenced — its method stays dead
    def unused_tally(self):
        return 2


BOOK = Ledger()
REGISTRY = Probe()
HOOK = _bump


def reads_book():
    return BOOK.tally()


try:
    use_package_surface()
    reads_book()
    lazy_import_caller()
except Exception:
    pass
