// issue #20 T2: functions reachable only through &fn / &Cls::fn pointers
// (non-ClassDB registrars) must not read as dead.
class Helper {
public:
	void work() {}
	void bind_methods() {
		ClassDB::bind_method(D_METHOD("dispatch"), &Helper::dispatch);
	}
	void dispatch() {
		register_cb(&Helper::work);      // qualified field-expression ref
		register_cb(&missing_thing);     // free-fn ref, resolved same file
		register_cb(&external_hook);     // never defined here: name-literal
	}
};

void missing_thing() {}
void unused_cb_helper() {}
