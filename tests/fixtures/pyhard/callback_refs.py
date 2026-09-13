"""callback_refs: bare-name argument references keep same-file defs
alive (issue #177) — a callback passed by reference has no call site,
so liveness must read the argument position itself. Strict guards: only
plain identifier tokens in argument position count; string contents and
attribute references (obj.method) never do.
"""
import atexit
import json

PAYLOAD = '{"n": 1}'


#- @no_constants defines func
def no_constants(x):
    raise AssertionError(f"strict JSON violated: {x}")


# module-scope keyword-arg ref (the #96/#177 shape from test_bakeint):
try:
    json.loads(PAYLOAD, parse_constant=no_constants)
except AssertionError:
    pass


#- @rank_rows defines func
def rank_rows(row):
    return row[1]


def sort_records(rows):
    # keyword-arg callback: key= has no call syntax anywhere
    return sorted(rows, key=rank_rows)


#- @flush_caches defines func
def flush_caches():
    return 0


# bare positional ref: register(f) passes the def itself
atexit.register(flush_caches)


class PageHandler:
    # handler-class shape: the class object rides an argument into
    # serving machinery that instantiates + dispatches reflectively
    def handle_request(self, req):
        return self.render(req)

    def render(self, req):
        return req


def make_listener(addr):
    # class-in-argument dispatch surface (#153/#164 tier convention):
    # body-level class refs are as much runtime dispatch as module ones
    return make_server(addr, PageHandler)


def serve():
    records = sort_records([("a", 2), ("b", 1)])
    return records, make_listener(("127.0.0.1", 0))


serve()


# --- guards (over-attribution teeth) --------------------------------------
def ghost_from_string():
    # name appears ONLY inside a string literal argument — not a ref
    return 1


apply_policy("ghost_from_string")


def fire_later():
    # referenced ONLY as an attribute (SCHED.fire_later) — not a ref
    return 2


SCHED = None
defer(SCHED.fire_later)


#- @unused_callback dead
def unused_callback():
    return 3
