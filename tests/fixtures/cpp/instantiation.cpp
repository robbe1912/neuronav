// issue #20 T3: memnew(T) is the engine's heap-construction idiom; `new T`
// and stack forms give the same class-level instantiation edge.
#include "provider.h"

static Provider *make_widget() {
	return memnew(Provider);
}

static Provider *make_plain() {
	return new Provider;
}

static void unused_inst_helper() {}
