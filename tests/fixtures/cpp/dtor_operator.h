// issue #20 T5: destructor / operator / conversion-operator names must
// harvest as Funcs on the inline path (qualified defs already worked).
class DtorOp {
public:
//- member total : int
	int total = 0;

//- @~DtorOp defines func
	~DtorOp() { total = 0; }

//- @operator+ defines func
	DtorOp operator+(const DtorOp &p_other) const {
		DtorOp r;
		r.total = total + p_other.total;
		return r;
	}

	operator bool() const { return total != 0; }
};
