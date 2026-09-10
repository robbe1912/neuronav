// fixture 1 (spec §5): macro-after-GDCLASS class body — method extraction
// must survive the GDCLASS line (tree-sitter error recovery leaves a
// benign MISSING-';' token); bound method alive, unused helper dead.
#include <vector>

class MacroWidget : public Object {
	GDCLASS(MacroWidget, Object)

protected:
	String label;
	void unused_private_helper(int p_x) { label = p_x; }

public:
	void refresh() { label = ""; }
	static void _bind_methods() {
		ClassDB::bind_method(D_METHOD("refresh"), &MacroWidget::refresh);
	}
};
