// issue #20 T2: functions reachable only through &fn / &Cls::fn pointers
// (non-ClassDB registrars) must not read as dead.
class Helper {
public:
//- @work defines func
	void work() {}
//- @bind_methods defines func
	void bind_methods() {
		ClassDB::bind_method(D_METHOD("dispatch"), &Helper::dispatch);
	}
//- @dispatch defines func
//- @dispatch calls @Helper::work
//- @dispatch calls @missing_thing
	void dispatch() {
		register_cb(&Helper::work);      // qualified field-expression ref
		register_cb(&missing_thing);     // free-fn ref, resolved same file
		register_cb(&external_hook);     // never defined here: name-literal
	}
};

//- @missing_thing defines func
void missing_thing() {}
//- @unused_cb_helper defines func
//- @unused_cb_helper dead
void unused_cb_helper() {}
