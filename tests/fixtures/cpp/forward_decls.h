// fixture 7 (spec §5): forward declarations produce NO edges and no
// class_map entries; the quoted include is the exact import surface.
#include "provider.h"

class Used;

class Consumer {
	Used *used = nullptr;

public:
	void consume() {}
};
