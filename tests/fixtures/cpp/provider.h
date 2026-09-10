// shared base: carries the GDVIRTUAL declaration harvested into the
// repo-wide override set, a bound method, and the forward declaration
// consumed by forward_decls.h (fixture 7).
class Provider : public Object {
	GDCLASS(Provider, Object)

public:
	GDVIRTUAL1(_ready_like, String)

	int provide_value() const { return 42; }

	static void _bind_methods() {
		ClassDB::bind_method(D_METHOD("provide_value"), &Provider::provide_value);
	}
};

class Used;
