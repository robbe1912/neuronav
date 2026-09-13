# langsep LJ-3 pin (judge verdict): the dead-tier underscore rule consults
# VIRTUALS cross-language — a .py fn named like a Godot virtual on an
# unresolved base stays "likely" (shielded), while an un-shielded underscore
# fn and a py do_* stand-in land "review". Byte-exact hook splits must
# reproduce all three rows.


class Probe(BaseNotFound):
    def _process(self):
        return 1

    def _mystery_thing(self):
        return 2

    def do_get(self):
        return 3
