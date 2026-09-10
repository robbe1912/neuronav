// fixture 10 (issue #20): mention-count corroboration — the first helper
// below is only ever called on an unresolvable receiver in the companion
// caller fixture (the same-name ambiguity guard drops that site from
// referenced_names), so its second corpus mention is exactly the evidence
// the dead tier needs to stop claiming "likely dead". The second helper is
// mentioned nowhere but its own definition and stays the honest "likely
// dead" control.
#include "provider.h"

static int rescued_by_mentions(int p_x) {
	return p_x + 7;
}

static int mention_free_helper(int p_x) {
	return p_x - 7;
}
