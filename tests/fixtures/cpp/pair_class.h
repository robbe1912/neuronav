// fixture 2a (spec §5): header half of the .h<->.cpp pair. Declares the
// class; the .cpp's first quoted include points back here.
//- class PairClass
//- extends Object
class PairClass : public Object {
	GDCLASS(PairClass, Object)

public:
//- !@combine defines func
	int combine(int p_a, int p_b);
	static void _bind_methods();
};
