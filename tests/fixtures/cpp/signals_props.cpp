// fixture 5 (spec §5): registration macro surface — ADD_SIGNAL lands in
// signals, ADD_PROPERTY adds the member plus set/get accessor edges,
// BIND_ENUM_CONSTANT lands in consts.
//- class SignalsProps
//- extends Object
class SignalsProps : public Object {
	GDCLASS(SignalsProps, Object)

public:
	enum Mode {
		MODE_FAST,
		MODE_SLOW,
	};

protected:
//- member label : String
	String label;

public:
//- @set_text defines func
//- entry set_text
	void set_text(const String &p_text) { label = p_text; }
//- @get_text defines func
//- entry get_text
	String get_text() const { return label; }

//- @_bind_methods defines func
//- signal value_changed
//- member text
//- const MODE_FAST
//- const MODE_SLOW
	static void _bind_methods() {
		ClassDB::bind_method(D_METHOD("set_text", "text"), &SignalsProps::set_text);
		ClassDB::bind_method(D_METHOD("get_text"), &SignalsProps::get_text);
		ADD_SIGNAL(MethodInfo("value_changed", PropertyInfo(Variant::INT, "value")));
		ADD_PROPERTY(PropertyInfo(Variant::STRING, "text"), "set_text", "get_text");
		BIND_ENUM_CONSTANT(MODE_FAST);
		BIND_ENUM_CONSTANT(MODE_SLOW);
	}
};
