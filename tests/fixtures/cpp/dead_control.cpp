// fixture 9 (spec §5): control group — unbound, non-virtual, uncalled
// functions in a file without any dynamic-dispatch marker are the only
// honest "likely dead" tier for C++.
#include "provider.h"

static int orphan_calc(int p_x) {
	return p_x * 3;
}

void unused_plain_helper() {}
