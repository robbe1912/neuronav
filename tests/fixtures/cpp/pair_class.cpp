// fixture 2b (spec §5): qualified out-of-line definitions — the engine's
// dominant .cpp shape. First quoted include = its own header (pairing).
//- include pair_class.h
#include "pair_class.h"

//- @combine defines func
//- entry combine
int PairClass::combine(int p_a, int p_b) {
	return p_a + p_b;
}

//- @_bind_methods defines func
void PairClass::_bind_methods() {
	ClassDB::bind_method(D_METHOD("combine", "a", "b"), &PairClass::combine);
}
