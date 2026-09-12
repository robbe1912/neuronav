// fixture 11 (spec §5, issue #110): plain-identifier call sites. The
// query had no free-function arm — helper(x) minted no call_expression
// capture, so file-local helpers died as false 'review' (definition +
// dropped call == mention floor) while qualified calls rescued theirs.
// The bound caller roots the plain helper at head; the uncalled control
// keeps the honest likely tier.
//- class PlainCaller
//- extends Object
class PlainCaller : public Object {
	GDCLASS(PlainCaller, Object)

public:
//- @run_plain defines func
//- entry run_plain
	void run_plain(int p_x) {
		int r = plain_helper(p_x);
		r += paired_helper(r);
	}
//- @_bind_methods defines func
	static void _bind_methods() {
		ClassDB::bind_method(D_METHOD("run_plain", "p_x"), &PlainCaller::run_plain);
	}
};

//- @plain_helper defines func
//- @run_plain calls @plain_helper
static int plain_helper(int p_x) {
	return p_x * 2 + 1;
}

//- @paired_helper defines func
//- @run_plain calls @paired_helper
static int paired_helper(int p_x) {
	return p_x - 3;
}

//- @unused_plain_fn defines func
//- @unused_plain_fn dead
static void unused_plain_fn() {}
