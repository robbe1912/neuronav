"""Fixture: __all__ exports are module entry roots (public API surface).

public_api/public_two have no module-level caller — the repo imports
them from elsewhere, which the extractor cannot see. The quoted names
in __all__ are the declared export surface and must root them.
"""


#- @public_api defines func
#- entry public_api
def public_api(a: int) -> int:
    return a + 1


#- @public_two defines func
#- entry public_two
#- @public_two calls @public_api
def public_two(a: int) -> int:
    return public_api(a) * 2


#- @private_helper defines func
#- @private_helper dead
#- !entry private_helper
def private_helper(a: int) -> int:
    return a - 1


__all__ = [
    "public_api",
    "public_two",
]
