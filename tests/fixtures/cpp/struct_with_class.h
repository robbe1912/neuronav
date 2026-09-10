// issue #20 V5: engine headers open with POD structs and close with the
// registered class — the class specifier wins the file's identity.
struct Pod {
	float raw = 0.0f;
};

class Real : public Pod {
public:
	int weight = 0;

	int heavy() const { return weight > 10 ? 1 : 0; }
};
