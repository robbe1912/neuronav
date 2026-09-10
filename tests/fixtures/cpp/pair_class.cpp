// fixture 2b (spec §5): qualified out-of-line definitions — the engine's
// dominant .cpp shape. First quoted include = its own header (pairing).
#include "pair_class.h"

int PairClass::combine(int p_a, int p_b) {
	return p_a + p_b;
}

void PairClass::_bind_methods() {
	ClassDB::bind_method(D_METHOD("combine", "a", "b"), &PairClass::combine);
}
