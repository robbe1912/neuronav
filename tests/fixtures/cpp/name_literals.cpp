// fixture 8 (spec §5): emit_signal(CoreStringName(x)) harvests a dispatch
// name literal; a same-named function anywhere in the repo stays alive.
//- include provider.h
#include "provider.h"

//- @state_changed defines func
//- literal state_changed
static void state_changed() {}

//- @dispatch_change defines func
//- @dispatch_change calls @emit_signal
void dispatch_change(Object *p_obj) {
	p_obj->emit_signal(CoreStringName(state_changed));
}

//- @unused_literal_helper defines func
//- @unused_literal_helper dead
void unused_literal_helper() {}
