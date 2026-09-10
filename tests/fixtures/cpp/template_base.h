// issue #20 T4: `public TBase<int>` must reduce to extends="TBase" so the
// base resolves through class_map like any plain base.
template <typename T>
class TBase;

class UsesTBase : public TBase<int> {
public:
	int doubled = 0;
};

template <typename T>
class TBase {
public:
	T base_val{};
};
