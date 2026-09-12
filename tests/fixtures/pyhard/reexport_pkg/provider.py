"""reexport_pkg.provider: submodule defining the re-exported helpers."""


def alpha(n):
    return n + 1


def beta(n):
    return n * 2


def gamma(n):
    return n - 1


def delta(n):
    return n // 2


def orphan(n):
    # control: defined but never imported by anyone — stays dead
    return 0
