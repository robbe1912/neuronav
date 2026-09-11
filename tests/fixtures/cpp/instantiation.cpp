// issue #20 T3: memnew(T) is the engine's heap-construction idiom; `new T`
// and stack forms give the same class-level instantiation edge.
//- include provider.h
#include "provider.h"

//- @make_widget defines func
//- @make_widget news Provider
static Provider *make_widget() {
	return memnew(Provider);
}

//- @make_plain defines func
//- @make_plain news Provider
static Provider *make_plain() {
	return new Provider;
}

//- @unused_inst_helper defines func
//- @unused_inst_helper dead
static void unused_inst_helper() {}
