// fixture 10 companion (issue #20): a field call on a receiver type this
// corpus never defines — the ambiguity guard drops the site, but the raw
// mention still counts toward the callee's corpus mention total.
#include "provider.h"

struct RouteEntry {
	int weight;
};

static int route_weight(int p_id) {
	RouteEntry entry{p_id};
	return entry.rescued_by_mentions(p_id);
}
