// fixture 8 (spec §5): emit_signal(CoreStringName(x)) harvests a dispatch
// name literal; a same-named function anywhere in the repo stays alive.
#include "provider.h"

static void state_changed() {}

void dispatch_change(Object *p_obj) {
	p_obj->emit_signal(CoreStringName(state_changed));
}

void unused_literal_helper() {}
