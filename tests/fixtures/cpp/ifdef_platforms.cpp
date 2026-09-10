// fixture 4 (spec §5): all #ifdef branches are collected — the extractor
// does no preprocessing; both branch bodies parse, deterministically.
#include "provider.h"

static int helper_calc(int p_x) {
	return p_x + 1;
}

#ifdef TOOLS_ENABLED
int tools_only_calc(int p_x) {
	return helper_calc(p_x) * 2;
}
#else
int runtime_only_calc(int p_x) {
	return helper_calc(p_x) * 3;
}
#endif

int unused_platform_helper() {
	return 7;
}
