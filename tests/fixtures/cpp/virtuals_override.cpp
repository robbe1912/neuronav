// fixture 6 (spec §5): engine-dispatched overrides are never dead —
// _notification via the frozen CPP_VIRTUALS set, _ready_like via the
// repo-harvested GDVIRTUAL set declared in provider.h.
#include "provider.h"

class OverridesThing : public Provider {
	GDCLASS(OverridesThing, Provider)

public:
	void _notification(int p_what) {
		if (p_what == 0) {
			return;
		}
	}

	void _ready_like(const String &p_arg) {
		_notification(1);
	}

	void unused_virtual_helper() {}
};
