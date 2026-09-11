// issue #20 T6: unused file-scope statics are honest dead material — they
// must surface in fs.globals; function locals must not.
//- global s_counter
static int s_counter = 0;
//- global kLimit
const int kLimit = 8;

static void bump() {
	s_counter += 1;
//- !global local_only
	int local_only = s_counter;
	(void)local_only;
}
