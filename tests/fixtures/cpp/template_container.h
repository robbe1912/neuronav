// fixture 3 (spec §5): inline template methods — template_declaration wraps
// the class; extraction must reach the inner methods without ERROR damage.
template <typename T>
class Vector {
	CowData<T> _cowdata;

public:
	void push_back(const T &p_value) { _cowdata.push(p_value); }
	const T &get(int p_index) const { return _cowdata.get(p_index); }
	void unused_tplate_helper() const {}
};
