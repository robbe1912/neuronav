// issue #20 T1: templated call sites sit in template_function /
// template_method nodes, not identifier/field_expression — without the
// dedicated arms every templated call edge is missing.
template <typename T>
T maxf(T p_a, T p_b) { return p_a > p_b ? p_a : p_b; }

class Calc {
	int value = 0;

public:
	template <typename T>
	T compute(T p_x) { return static_cast<T>(value) + p_x; }

	void run() {
		int r = maxf<int>(3, 4);
		float f = compute<float>(1.5f);
		(void)r;
		(void)f;
	}

	void bind_methods() {
		ClassDB::bind_method(D_METHOD("run"), &Calc::run);
	}

	void unused_tcall_helper() {}
};
