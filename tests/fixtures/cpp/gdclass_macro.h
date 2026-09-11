// fixture 1 (spec §5): macro-after-GDCLASS class body — method extraction
// must survive the GDCLASS line (tree-sitter error recovery leaves a
// benign MISSING-';' token); bound method alive, unused helper dead.
//- !include vector
#include <vector>

//- class MacroWidget
//- extends Object
class MacroWidget : public Object {
	GDCLASS(MacroWidget, Object)

protected:
//- member label : String
	String label;
//- @unused_private_helper defines func
//- @unused_private_helper dead
	void unused_private_helper(int p_x) { label = p_x; }

public:
//- @refresh defines func
//- entry refresh
	void refresh() { label = ""; }
//- @_bind_methods defines func
	static void _bind_methods() {
		ClassDB::bind_method(D_METHOD("refresh"), &MacroWidget::refresh);
	}
};
